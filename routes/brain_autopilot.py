"""Phase AAA (2026-05-16) — Brain autopilot.

The brain has detectors + findings + Layer 5 proposals — but the proposal
loop ONLY produces code suggestions for human review. For operational
issues (stale cron needs retrigger, ISO loop needs restart, stale cache
needs purge), there's no autonomous action layer. So the brain "knows"
about problems but does nothing about them. This module is the doer.

Architecture:
  1. Pattern library: finding-issue → action callable
  2. Every 30 min, cron POSTs /api/v1/brain/autopilot/run
  3. Reads /api/v1/heal/findings actionable_backend_issues
  4. For each finding matching a known pattern:
     a. Rate-limit check (max N actions/pattern/hour)
     b. Idempotency check (was this finding already acted on in the
        last cooldown window?)
     c. Execute the action
     d. Log to brain_autopilot_actions table with outcome
  5. If same pattern fires N+ times in 24h despite remediation,
     escalate to humans (mark `escalated=true`)

Safety:
  - HARD KILL SWITCH: env var BRAIN_AUTOPILOT_DISABLED=1 blocks all actions
  - DRY RUN: env var BRAIN_AUTOPILOT_DRY_RUN=1 logs intended actions, doesn't execute
  - WHITELIST ONLY: only finding-issue strings explicitly mapped here can fire
  - Rate limit: 3 actions per pattern per hour, 10 per 24h, hard-coded
  - Cooldown: 30 min between same-finding actions
  - Audit: every action gets a brain_autopilot_actions row

Endpoints:
  POST /api/v1/brain/autopilot/run     — admin or internal-key gated; cron entry point
  GET  /api/v1/brain/autopilot/status   — public; last 24h activity summary
  GET  /api/v1/brain/autopilot/library  — public; the pattern→action map
"""

from __future__ import annotations

import os
import json
import urllib.request
import datetime
from typing import Any
from flask import Blueprint, jsonify, request
import psycopg2
import psycopg2.extras


brain_autopilot_bp = Blueprint("brain_autopilot", __name__)


# Rate limits and safety caps
_MAX_ACTIONS_PER_PATTERN_PER_HOUR = 3
_MAX_ACTIONS_PER_PATTERN_PER_24H  = 10
_COOLDOWN_MIN_BETWEEN_SAME_ACTIONS = 30   # minutes
_ESCALATION_THRESHOLD_24H          = 5     # if pattern fires this many times
                                            # in 24h despite remediation,
                                            # mark escalated and stop acting

# Where Railway lives — autopilot calls back into ourselves to retrigger
# operations. CF Worker's 15s timeout is a no-go for long actions.
_BACKEND_BASE = os.environ.get(
    "DCHUB_BACKEND_BASE",
    "https://dchub-backend-production.up.railway.app",
)


def _is_disabled() -> bool:
    return str(os.environ.get("BRAIN_AUTOPILOT_DISABLED", "")).lower() in ("1", "true", "yes")


def _is_dry_run() -> bool:
    return str(os.environ.get("BRAIN_AUTOPILOT_DRY_RUN", "")).lower() in ("1", "true", "yes")


def _admin_key() -> str | None:
    return os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")


def _conn():
    db = os.environ.get("DATABASE_URL")
    if not db: return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


def _ensure_schema():
    """Idempotent. Stores every autopilot action attempt."""
    c = _conn()
    if c is None: return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS brain_autopilot_actions (
                    id              BIGSERIAL PRIMARY KEY,
                    finding_issue   TEXT NOT NULL,
                    finding_url     TEXT,
                    pattern_name    TEXT NOT NULL,
                    action_endpoint TEXT,
                    action_payload  JSONB,
                    dry_run         BOOLEAN NOT NULL DEFAULT FALSE,
                    outcome         TEXT,
                    http_code       INT,
                    response_body   TEXT,
                    error           TEXT,
                    escalated       BOOLEAN NOT NULL DEFAULT FALSE,
                    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    completed_at    TIMESTAMPTZ
                );
                CREATE INDEX IF NOT EXISTS ix_autopilot_recent
                    ON brain_autopilot_actions(started_at DESC);
                CREATE INDEX IF NOT EXISTS ix_autopilot_pattern
                    ON brain_autopilot_actions(pattern_name, started_at DESC);
            """)
        return True
    except Exception as e:
        print(f"[autopilot] schema init failed: {e}")
        return False
    finally:
        try: c.close()
        except Exception: pass


try: _ensure_schema()
except Exception: pass


# ── ACTION LIBRARY ────────────────────────────────────────────────────
# pattern_name → callable that returns (action_endpoint, payload_dict).
# Returning (None, None) means "no actionable remediation; log only."
#
# Each action MUST be idempotent and safe to retry. No deletes, no
# destructive ops. Re-trigger crons, refresh caches, retry imports — yes.
# Anything that touches money, auth, or user data — NO.

def _action_dcpi_partial_recompute(finding: dict) -> tuple[str | None, dict | None]:
    """DCPI median age > 48h. Re-trigger the recompute with chunking
    so the next read shows fresh data. Idempotent — recompute upserts.
    """
    return "/api/v1/dcpi/recompute?offset=0&limit=100", {}


def _action_discovery_stalled(finding: dict) -> tuple[str | None, dict | None]:
    """Zero new facilities in 7d. We don't have a one-shot endpoint to
    re-fire all discovery crons; log a high-priority escalation marker
    so a human triggers the workflow_dispatch on dchub-osm-refresh.yml
    + daily-infra-sync.yml. Marked escalate=True so it doesn't burn
    rate limit retrying."""
    return None, None   # escalation-only


def _action_iso_metric_zero(finding: dict) -> tuple[str | None, dict | None]:
    """Per-ISO 0 metrics in 24h. The grid loops are workflow-driven —
    we don't have an in-process trigger. Log + escalate so a human
    fires the iso-data-pull.yml workflow."""
    return None, None   # escalation-only


def _action_press_repetition(finding: dict) -> tuple[str | None, dict | None]:
    """Last 3 press releases same market. Force a new generation
    skipping the repeat market via the existing repost endpoint."""
    return "/api/v1/marketing/repost-now?force_topic_rotation=1", {}


def _action_mcp_conversion_stale(finding: dict) -> tuple[str | None, dict | None]:
    """No conversions on 500+ signals in 7d. No autonomous fix possible —
    this is a paywall-copy / pricing issue. Escalate only."""
    return None, None   # escalation-only


def _action_worker_version_drift(finding: dict) -> tuple[str | None, dict | None]:
    """CF Pages worker stale. Requires CF dashboard action — escalate."""
    return None, None


def _action_seo_sitemap_stale(finding: dict) -> tuple[str | None, dict | None]:
    """Sitemap lastmod stale. Re-trigger sitemap regeneration."""
    return "/api/v1/sitemap/regenerate", {}


# Phase CCC (2026-05-16): 5 more escalation patterns so the audit log
# shows the brain RECOGNIZING these findings (vs silently no_action-ing).
# Counted as escalated so the rate-limit doesn't burn cycles retrying;
# the human gets a clean view at /autopilot/recent of every flagged
# pattern and what was/wasn't auto-fixable.

def _action_radar_detector_crashed(finding: dict) -> tuple[str | None, dict | None]:
    """A detector raised an exception. Escalate — needs code fix."""
    return None, None


def _action_tier_inconsistency(finding: dict) -> tuple[str | None, dict | None]:
    """MCP↔web tier mismatch. Needs decorator change in source. Escalate."""
    return None, None


def _action_cron_missing_schedule(finding: dict) -> tuple[str | None, dict | None]:
    """workflow_dispatch-only phase. Needs YAML schedule add. Escalate."""
    return None, None


def _action_cron_schedule_collision(finding: dict) -> tuple[str | None, dict | None]:
    """Two workflows share the same cron minute. Needs YAML edit. Escalate."""
    return None, None


def _action_worker_source_unreachable(finding: dict) -> tuple[str | None, dict | None]:
    """raw.githubusercontent.com fetch failed (private repo, no token).
    Needs GITHUB_TOKEN env var. Escalate."""
    return None, None


_PATTERN_LIBRARY: dict[str, dict[str, Any]] = {
    "dcpi_partial_recompute": {
        "action":      _action_dcpi_partial_recompute,
        "method":      "POST",
        "use_admin":   True,
        "description": "Retrigger DCPI recompute with chunking (covers 100 markets per attempt)",
    },
    "discovery_stalled_7d": {
        "action":      _action_discovery_stalled,
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: humans must trigger dchub-osm-refresh.yml workflow",
    },
    "iso_metric_count_zero_24h": {
        "action":      _action_iso_metric_zero,
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: humans must trigger iso-data-pull.yml workflow",
    },
    "iso_metric_count_dropped": {
        "action":      _action_iso_metric_zero,
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: per-ISO partial loop failure needs human investigation",
    },
    "auto_press_market_repetition": {
        "action":      _action_press_repetition,
        "method":      "POST",
        "use_admin":   True,
        "description": "Force a new press release with topic rotation enabled",
    },
    "mcp_conversion_stale_critical": {
        "action":      _action_mcp_conversion_stale,
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: paywall/pricing review needed (not autonomous)",
    },
    "worker_version_drift": {
        "action":      _action_worker_version_drift,
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: requires CF dashboard 'promote to production'",
    },
    "seo_sitemap_stale": {
        "action":      _action_seo_sitemap_stale,
        "method":      "POST",
        "use_admin":   True,
        "description": "Retrigger sitemap regeneration",
    },
    # Phase CCC (2026-05-16): 5 more escalation-only patterns.
    "consistency_radar_detector_crashed": {
        "action":      _action_radar_detector_crashed,
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: a brain detector raised — code fix needed (check the detector's SQL)",
    },
    "tier_inconsistency_web_higher_than_mcp": {
        "action":      _action_tier_inconsistency,
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: web endpoint blocks anonymous while MCP allows — needs decorator alignment",
    },
    "cron_phase_missing_schedule": {
        "action":      _action_cron_missing_schedule,
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: workflow_dispatch-only phase needs a cron: schedule added to its .yml",
    },
    "cron_schedule_collision": {
        "action":      _action_cron_schedule_collision,
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: two workflows fire the same minute — stagger one in its .yml",
    },
    "worker_source_unreachable": {
        "action":      _action_worker_source_unreachable,
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: GITHUB_TOKEN env var missing — radar can't fetch private _worker.js source",
    },
}


# Phase CCC: prefix-match library lookup. Detector crashes have dynamic
# issue keys like "consistency_radar_detector_crashed:check_xxx" so we
# also try the bare prefix for autopilot pattern matching.
def _lookup_pattern(issue: str) -> dict | None:
    """Direct lookup first, then prefix match (issue.split(':',1)[0])."""
    if not issue: return None
    direct = _PATTERN_LIBRARY.get(issue)
    if direct: return direct
    base = issue.split(":", 1)[0]
    if base != issue:
        return _PATTERN_LIBRARY.get(base)
    return None


# ── Rate limit helpers ────────────────────────────────────────────────
def _recent_actions(cur, pattern: str, hours: int) -> int:
    cur.execute("""
        SELECT COUNT(*) FROM brain_autopilot_actions
         WHERE pattern_name = %s
           AND started_at >= NOW() - INTERVAL %s
    """, (pattern, f"{hours} hours"))
    return int((cur.fetchone() or [0])[0] or 0)


def _last_action_age_minutes(cur, pattern: str, url: str | None) -> int | None:
    cur.execute("""
        SELECT EXTRACT(EPOCH FROM (NOW() - MAX(started_at))) / 60
          FROM brain_autopilot_actions
         WHERE pattern_name = %s
           AND COALESCE(finding_url,'') = %s
    """, (pattern, url or ""))
    r = cur.fetchone()
    if not r or r[0] is None: return None
    return int(float(r[0]))


def _rate_limit_check(cur, pattern: str, url: str | None) -> tuple[bool, str]:
    """Return (allowed, reason)."""
    last_age = _last_action_age_minutes(cur, pattern, url)
    if last_age is not None and last_age < _COOLDOWN_MIN_BETWEEN_SAME_ACTIONS:
        return False, f"cooldown_active ({last_age}min < {_COOLDOWN_MIN_BETWEEN_SAME_ACTIONS}min)"
    n_hr = _recent_actions(cur, pattern, 1)
    if n_hr >= _MAX_ACTIONS_PER_PATTERN_PER_HOUR:
        return False, f"hourly_limit ({n_hr} >= {_MAX_ACTIONS_PER_PATTERN_PER_HOUR})"
    n_24h = _recent_actions(cur, pattern, 24)
    if n_24h >= _MAX_ACTIONS_PER_PATTERN_PER_24H:
        return False, f"daily_limit ({n_24h} >= {_MAX_ACTIONS_PER_PATTERN_PER_24H})"
    if n_24h >= _ESCALATION_THRESHOLD_24H:
        return False, f"escalation_threshold ({n_24h} >= {_ESCALATION_THRESHOLD_24H}) — needs human"
    return True, "ok"


# ── Action executor ───────────────────────────────────────────────────
def _execute_action(action_path: str, payload: dict, use_admin: bool) -> tuple[int | None, str | None, str | None]:
    """Returns (http_code, response_body_snippet, error_string)."""
    if _is_dry_run():
        return 0, "dry-run: action NOT executed", None
    url = _BACKEND_BASE.rstrip("/") + action_path
    try:
        data = json.dumps(payload or {}).encode("utf-8") if payload else b"{}"
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        if use_admin:
            ak = _admin_key()
            if ak: req.add_header("X-Admin-Key", ak)
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, body[:400], None
    except urllib.error.HTTPError as e:
        try: body = e.read().decode("utf-8", errors="replace")[:400]
        except Exception: body = ""
        return e.code, body, f"HTTPError {e.code} {e.reason}"
    except Exception as e:
        return None, "", f"{type(e).__name__}: {str(e)[:200]}"


def _record_action(finding: dict, pattern: str, action_path: str | None,
                    payload: dict | None, dry_run: bool, escalated: bool,
                    http_code: int | None, body: str | None, error: str | None,
                    outcome: str):
    c = _conn()
    if c is None: return
    try:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO brain_autopilot_actions
                    (finding_issue, finding_url, pattern_name,
                     action_endpoint, action_payload, dry_run, outcome,
                     http_code, response_body, error, escalated,
                     started_at, completed_at)
                VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,NOW(),NOW())
            """, (
                str(finding.get("issue",""))[:200],
                str(finding.get("url",""))[:500],
                pattern[:100],
                (action_path or "")[:500],
                json.dumps(payload or {}),
                bool(dry_run), outcome[:80],
                http_code, (body or "")[:1000], (error or "")[:500],
                bool(escalated),
            ))
    except Exception as e:
        print(f"[autopilot] record_action failed: {e}")
    finally:
        try: c.close()
        except Exception: pass


# ── ENDPOINTS ─────────────────────────────────────────────────────────
@brain_autopilot_bp.route("/api/v1/brain/autopilot/run", methods=["POST"])
def autopilot_run():
    """Cron entry point. Reads /api/v1/heal/findings, executes safe
    actions for known patterns. Admin or internal-key gated."""
    expected = _admin_key()
    provided = request.headers.get("X-Admin-Key") or request.args.get("admin_key")
    if expected and provided != expected:
        return jsonify(error="unauthorized"), 401

    if _is_disabled():
        return jsonify(ok=True, skipped=True,
                       reason="BRAIN_AUTOPILOT_DISABLED env set"), 200

    # Pull findings from our OWN heal endpoint via Railway-direct (bypass
    # CF Worker timeout). This is the same JSON the brain reads.
    try:
        req = urllib.request.Request(
            _BACKEND_BASE.rstrip("/") + "/api/v1/heal/findings",
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return jsonify(error="heal_findings_fetch_failed",
                       detail=str(e)[:200]), 500

    issues = payload.get("actionable_backend_issues") or []
    if not isinstance(issues, list):
        issues = []

    summary = {
        "examined":   len(issues),
        "actioned":   0,
        "rate_limited": 0,
        "escalated":  0,
        "no_action":  0,
        "errors":     0,
        "dry_run":    _is_dry_run(),
        "actions":    [],
    }

    c = _conn()
    if c is None:
        return jsonify(error="no_database"), 503
    try:
        for f in issues:
            if not isinstance(f, dict): continue
            issue = f.get("issue") or ""
            pat = _lookup_pattern(issue)
            if not pat:
                summary["no_action"] += 1
                continue

            with c.cursor() as cur:
                allowed, reason = _rate_limit_check(cur, issue, f.get("url"))

            action_fn = pat["action"]
            action_path, payload_body = action_fn(f)

            if not allowed:
                summary["rate_limited"] += 1
                escalated = ("escalation_threshold" in reason)
                if escalated: summary["escalated"] += 1
                _record_action(f, issue, action_path, payload_body,
                                dry_run=_is_dry_run(), escalated=escalated,
                                http_code=None, body=None, error=reason,
                                outcome="rate_limited")
                continue

            if action_path is None:
                # Escalation-only pattern; log and continue
                summary["escalated"] += 1
                _record_action(f, issue, None, None,
                                dry_run=_is_dry_run(), escalated=True,
                                http_code=None, body=None,
                                error="no autonomous action for this pattern",
                                outcome="escalated")
                continue

            # Execute
            http_code, body, error = _execute_action(action_path,
                                                       payload_body or {},
                                                       use_admin=pat.get("use_admin", False))
            outcome = "executed_ok" if (http_code and 200 <= http_code < 300) else (
                "dry_run" if _is_dry_run() else "execution_failed")
            if outcome == "execution_failed": summary["errors"] += 1
            else: summary["actioned"] += 1
            _record_action(f, issue, action_path, payload_body,
                            dry_run=_is_dry_run(), escalated=False,
                            http_code=http_code, body=body, error=error,
                            outcome=outcome)
            summary["actions"].append({
                "issue": issue, "pattern": issue, "endpoint": action_path,
                "outcome": outcome, "http_code": http_code,
            })
    finally:
        try: c.close()
        except Exception: pass

    summary["completed_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    return jsonify(ok=True, summary=summary), 200


@brain_autopilot_bp.route("/api/v1/brain/autopilot/status", methods=["GET"])
def autopilot_status():
    """Public summary of autopilot activity over the last 24h."""
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT pattern_name, outcome,
                       COUNT(*) AS n,
                       MAX(started_at) AS last_at
                  FROM brain_autopilot_actions
                 WHERE started_at >= NOW() - INTERVAL '24 hours'
                 GROUP BY pattern_name, outcome
                 ORDER BY n DESC
            """)
            rows = cur.fetchall()
            cur.execute("""
                SELECT COUNT(*) FROM brain_autopilot_actions
                 WHERE started_at >= NOW() - INTERVAL '24 hours'
            """)
            total = int((cur.fetchone() or {"count": 0})["count"] or 0)
            cur.execute("""
                SELECT MAX(started_at) FROM brain_autopilot_actions
            """)
            last_ever_row = cur.fetchone()
            last_ever = last_ever_row["max"].isoformat() if last_ever_row and last_ever_row["max"] else None
    finally:
        try: c.close()
        except Exception: pass

    for r in rows:
        if r.get("last_at"): r["last_at"] = r["last_at"].isoformat()

    return jsonify(
        actions_24h=total,
        last_action_ever=last_ever,
        by_pattern_outcome=rows,
        disabled=_is_disabled(),
        dry_run=_is_dry_run(),
        pattern_library_size=len(_PATTERN_LIBRARY),
    ), 200


@brain_autopilot_bp.route("/api/v1/brain/autopilot/library", methods=["GET"])
def autopilot_library():
    """Public — the pattern→action map. So agents + humans can see what
    the autopilot will and won't do."""
    out = []
    for pat, meta in _PATTERN_LIBRARY.items():
        out.append({
            "finding_issue":      pat,
            "method":              meta.get("method"),
            "autonomous":          meta.get("method") is not None,
            "description":         meta.get("description"),
            "uses_admin_key":      meta.get("use_admin", False),
        })
    return jsonify(
        patterns=out,
        total_patterns=len(out),
        autonomous_count=sum(1 for p in out if p["autonomous"]),
        escalation_only_count=sum(1 for p in out if not p["autonomous"]),
        rate_limits={
            "max_per_pattern_per_hour": _MAX_ACTIONS_PER_PATTERN_PER_HOUR,
            "max_per_pattern_per_24h":  _MAX_ACTIONS_PER_PATTERN_PER_24H,
            "cooldown_minutes":          _COOLDOWN_MIN_BETWEEN_SAME_ACTIONS,
            "escalation_threshold_24h":  _ESCALATION_THRESHOLD_24H,
        },
        kill_switch_env="BRAIN_AUTOPILOT_DISABLED",
        dry_run_env="BRAIN_AUTOPILOT_DRY_RUN",
        disabled=_is_disabled(),
        dry_run=_is_dry_run(),
    ), 200


@brain_autopilot_bp.route("/api/v1/brain/heartbeat", methods=["GET"])
def brain_heartbeat():
    """ONE endpoint showing the brain's actual proactivity state.

    This is the canonical "is the brain doing anything?" probe. Returns:
      - Detector freshness (last consistency-radar scan + finding count)
      - Heal cache state (memory + DB age)
      - Autopilot activity (last 24h actions, by outcome)
      - Layer 5 activity (last code-proposal cron run)
      - Overall verdict: alive | warming | blind | dormant
    """
    out: dict = {
        "checked_at": datetime.datetime.utcnow().isoformat() + "Z",
        "verdict":    "unknown",
    }
    c = _conn()

    # ── Detector freshness ──
    try:
        from routes.brain_consistency_radar import scan_summary as _scan
        s = _scan()
        out["detector"] = {
            "findings_count": int(s.get("count") or 0),
            "by_issue":       s.get("by_issue") or {},
            "as_of":          s.get("as_of"),
        }
    except Exception as e:
        out["detector"] = {"error": str(e)[:160]}

    # ── Heal cache freshness ──
    try:
        if c:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT EXTRACT(EPOCH FROM (NOW() - MAX(computed_at)))::int AS age_s,
                           COUNT(*) AS total_persisted
                      FROM heal_findings_cache
                """)
                r = cur.fetchone()
                if r:
                    out["heal_cache"] = {
                        "db_age_seconds":  int(r[0]) if r[0] is not None else None,
                        "persisted_rows":  int(r[1] or 0),
                    }
    except Exception as e:
        out["heal_cache"] = {"error": str(e)[:160]}

    # ── Autopilot activity ──
    try:
        if c:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT
                      COUNT(*) FILTER (WHERE outcome = 'executed_ok'
                                        AND started_at >= NOW() - INTERVAL '24 hours') AS actioned_24h,
                      COUNT(*) FILTER (WHERE outcome = 'escalated'
                                        AND started_at >= NOW() - INTERVAL '24 hours') AS escalated_24h,
                      COUNT(*) FILTER (WHERE outcome = 'rate_limited'
                                        AND started_at >= NOW() - INTERVAL '24 hours') AS rate_limited_24h,
                      COUNT(*) FILTER (WHERE outcome = 'execution_failed'
                                        AND started_at >= NOW() - INTERVAL '24 hours') AS errors_24h,
                      MAX(started_at) AS last_action_at
                      FROM brain_autopilot_actions
                """)
                r = cur.fetchone() or {}
                out["autopilot"] = {
                    "actioned_24h":      int(r.get("actioned_24h") or 0),
                    "escalated_24h":     int(r.get("escalated_24h") or 0),
                    "rate_limited_24h":  int(r.get("rate_limited_24h") or 0),
                    "errors_24h":        int(r.get("errors_24h") or 0),
                    "last_action_at":    r["last_action_at"].isoformat() if r.get("last_action_at") else None,
                    "disabled":          _is_disabled(),
                    "dry_run":           _is_dry_run(),
                    "pattern_library_size": len(_PATTERN_LIBRARY),
                }
    except Exception as e:
        out["autopilot"] = {"error": str(e)[:160]}

    # ── Layer 5 activity ──
    try:
        if c:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT to_regclass('public.brain_proposed_code')
                """)
                if (cur.fetchone() or [None])[0]:
                    cur.execute("""
                        SELECT COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '24 hours'),
                               MAX(created_at)
                          FROM brain_proposed_code
                    """)
                    r = cur.fetchone()
                    if r:
                        out["layer5"] = {
                            "proposals_24h":  int(r[0] or 0),
                            "last_proposal":   r[1].isoformat() if r[1] else None,
                        }
    except Exception as e:
        out["layer5"] = {"error": str(e)[:160]}

    if c:
        try: c.close()
        except Exception: pass

    # ── Verdict ──
    det = out.get("detector") or {}
    hc  = out.get("heal_cache") or {}
    ap  = out.get("autopilot") or {}
    findings_count = det.get("findings_count", 0) or 0
    db_age_s = hc.get("db_age_seconds")
    actioned = ap.get("actioned_24h", 0) or 0
    escalated = ap.get("escalated_24h", 0) or 0
    if db_age_s is None:
        verdict = "blind"
        verdict_detail = "heal_findings_cache table never written — brain has never seen findings"
    elif db_age_s > 3600 * 6:
        verdict = "stale"
        verdict_detail = f"heal cache is {db_age_s/3600:.1f}h old — refresh cron likely failing"
    elif findings_count == 0 and actioned == 0 and escalated == 0:
        verdict = "dormant"
        verdict_detail = "no findings, no actions — either nothing's wrong OR detectors are bypassed"
    elif _is_disabled():
        verdict = "disabled"
        verdict_detail = "BRAIN_AUTOPILOT_DISABLED env set — autopilot off, detectors still firing"
    elif actioned + escalated > 0:
        verdict = "alive"
        verdict_detail = f"{actioned} autonomous fixes + {escalated} escalations in last 24h"
    else:
        verdict = "warming"
        verdict_detail = f"{findings_count} open findings, no actions in last 24h yet"
    out["verdict"] = verdict
    out["verdict_detail"] = verdict_detail
    return jsonify(out), 200


@brain_autopilot_bp.route("/api/v1/brain/autopilot/recent", methods=["GET"])
def autopilot_recent():
    """Public — last 50 autopilot actions (audit log)."""
    try: limit = max(1, min(200, int(request.args.get("limit") or 50)))
    except ValueError: limit = 50
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, finding_issue, finding_url, pattern_name,
                       action_endpoint, dry_run, outcome, http_code,
                       LEFT(COALESCE(response_body,''), 200) AS response_snippet,
                       error, escalated, started_at
                  FROM brain_autopilot_actions
                 ORDER BY started_at DESC
                 LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
    finally:
        try: c.close()
        except Exception: pass
    for r in rows:
        if r.get("started_at"): r["started_at"] = r["started_at"].isoformat()
    return jsonify(actions=rows, count=len(rows)), 200
