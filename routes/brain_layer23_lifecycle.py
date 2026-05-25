"""
brain_layer23_lifecycle.py — Phase r35 (2026-05-25).

Layer 23: Lifecycle Curator. The brain's proactive moat-builder.

(Layer 22 was taken by brain_layer22_auto_code, the GH-issue-drafting
auto-code module. This curator is the next layer up — it watches the
whole product lifecycle and proposes capabilities, not single fixes.)

Until now the brain has been REACTIVE — it reads /api/v1/heal/findings,
proposes text fixes, ships them. Layer 23 makes it PROACTIVE across
the site's entire lifecycle:

  1. Audits 10 moat dimensions on a recurring cron (every 2 hours)
     - server-card stats drift (claimed vs actual)
     - tool description quality
     - ai_citations week-over-week trend
     - press cadence vs target
     - topic-pulse health (suggestions firing?)
     - registry presence (Smithery / Glama / mcp.run / etc.)
     - platform activity (new platforms → active, going quiet)
     - DCPI freshness
     - OG meta currency
     - brain vocabulary growth (new error classes added)

  2. Calls Claude (Opus 4.7, reasoning tier) with the audit summary
     and asks for ONE creative new capability that would deepen the
     moat. Proposals get logged for human review — this is the
     "evolve to create new processes, expanded capabilities" loop.

  3. Surfaces findings via /api/v1/brain/lifecycle/findings (the
     dashboard tile + surveillance sweep consume it).

  4. Records proposals to brain_lifecycle_proposals so they persist
     across deploys and can be approved → implemented in future rounds.

Endpoints:
  GET  /api/v1/brain/lifecycle/audit       run audit + return findings
  GET  /api/v1/brain/lifecycle/findings    cached last-audit results
  POST /api/v1/brain/lifecycle/propose     admin: ask Opus for a new
                                           capability proposal
  GET  /api/v1/brain/lifecycle/proposals   list recent Opus proposals
"""
from __future__ import annotations

import datetime
import json
import os
import time
import urllib.request
import urllib.error
from typing import Any

from flask import Blueprint, jsonify, request, current_app


brain_lifecycle_bp = Blueprint("brain_lifecycle", __name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY")
             or os.environ.get("DCHUB_INTERNAL_KEY") or "")


# ── DB connection ─────────────────────────────────────────────────

def _conn():
    """Return Neon connection or None. Tries both env-var aliases."""
    db = (os.environ.get("DATABASE_URL")
          or os.environ.get("NEON_DATABASE_URL"))
    if not db:
        return None
    try:
        import psycopg2
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


def _ensure_schema():
    """Idempotent — table to persist proposals across deploys."""
    c = _conn()
    if c is None:
        return
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS brain_lifecycle_proposals (
                    id BIGSERIAL PRIMARY KEY,
                    proposed_at TIMESTAMPTZ DEFAULT NOW(),
                    audit_snapshot JSONB,
                    proposal_text TEXT,
                    proposal_kind TEXT,
                    model TEXT,
                    approved BOOLEAN DEFAULT NULL,
                    shipped_at TIMESTAMPTZ DEFAULT NULL,
                    notes TEXT
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS ix_blp_proposed_at "
                "ON brain_lifecycle_proposals (proposed_at DESC)"
            )
            # r42 (2026-05-25): proposal lifecycle columns. dismissed_at
            # for "not pursuing" decisions, reviewed_by for audit trail,
            # gh_issue_url + gh_issue_number for the L22 autonomy bridge.
            for col_def in [
                "dismissed_at TIMESTAMPTZ",
                "reviewed_by TEXT",
                "gh_issue_url TEXT",
                "gh_issue_number INTEGER",
                # r47 (2026-05-25): multi-model challenger fields
                "challenger_model TEXT",
                "challenger_approved BOOLEAN",
                "challenger_score INTEGER",
                "challenger_critique TEXT",
                # r48 (2026-05-25): persist challenger error so silent
                # failures (Sonnet API 4xx, JSON parse fail, network)
                # become visible in the proposals stream
                "challenger_error TEXT",
            ]:
                col_name = col_def.split()[0]
                try:
                    cur.execute(
                        f"ALTER TABLE brain_lifecycle_proposals "
                        f"ADD COLUMN IF NOT EXISTS {col_def}"
                    )
                except Exception:
                    pass
            # r45 (2026-05-25): lifecycle health history — snapshots
            # composite_health every audit run so the brain can detect
            # its own moat trajectory over time, not just point-in-time.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS brain_lifecycle_history (
                    id BIGSERIAL PRIMARY KEY,
                    at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    composite_health REAL NOT NULL,
                    findings_count INT NOT NULL DEFAULT 0,
                    unknown_count INT NOT NULL DEFAULT 0,
                    weak_dims JSONB,
                    elapsed_ms INT
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS ix_blh_at "
                "ON brain_lifecycle_history (at DESC)"
            )
    except Exception:
        pass
    finally:
        try: c.close()
        except Exception: pass


# r45 (2026-05-25): snapshot helper. Called by _run_full_audit at the
# tail of every fresh compose. Idempotent + best-effort — never raises.
# 5-min audit cache + per-audit-cycle snapshot means we get one row
# per actual compose, not per cache hit.
_HISTORY_LAST_SNAPSHOT_AT: float = 0.0
_HISTORY_MIN_GAP_SECONDS = 300.0


def _snapshot_history(composite: float, findings_count: int,
                       unknown_count: int, weak_dims: list,
                       elapsed_ms: int) -> None:
    """Persist a row to brain_lifecycle_history. Throttled to 5min."""
    global _HISTORY_LAST_SNAPSHOT_AT
    now = time.time()
    if (now - _HISTORY_LAST_SNAPSHOT_AT) < _HISTORY_MIN_GAP_SECONDS:
        return
    c = _conn()
    if c is None:
        return
    try:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO brain_lifecycle_history
                    (composite_health, findings_count, unknown_count,
                     weak_dims, elapsed_ms)
                VALUES (%s, %s, %s, %s, %s)
            """, (
                float(composite),
                int(findings_count),
                int(unknown_count),
                json.dumps(weak_dims),
                int(elapsed_ms),
            ))
        _HISTORY_LAST_SNAPSHOT_AT = now
    except Exception:
        pass
    finally:
        try: c.close()
        except Exception: pass


# ── audit signal collectors (one per moat dimension) ─────────────

# r38 (2026-05-25): sentinel value so audit functions can distinguish
# "endpoint returned empty/timeout" from "endpoint returned real data
# that happens to have None values". Without this, a single slow
# sub-endpoint marks its dim as 'weak' (since `score: None` fails the
# threshold check) — misleading; the dim is actually 'unknown'.
class _AuditUnavailable:
    """Marker that a sub-endpoint failed (timeout / non-200 / crash)."""
    __slots__ = ("path",)
    def __init__(self, path: str): self.path = path
    def __bool__(self): return False
    def get(self, *a, **kw): return None
    def __repr__(self): return f"<unavailable:{self.path}>"


# r43 (2026-05-25): 60s path→response cache. When multiple audit
# composes happen close together (cron + dashboard refresh + manual
# probe), the inner upstream hits hit cache instead of triggering
# fresh composes. Also helps when one audit dim is hit by multiple
# audit fns (e.g. /api/v1/mcp/growth feeds both platform_activity
# AND tool_conversion_health).
_INTERNAL_CACHE: dict = {}
_INTERNAL_TTL = 60.0


def _call_internal(path: str, timeout: float = 8.0):
    """In-process GET, 60s memoized by path.

    r44 (2026-05-25): DIRECT view-function dispatch — bypasses
    test_client + WSGI middleware entirely. Looks up the endpoint
    via url_map.match(), grabs the view from app.view_functions,
    and invokes it inside a test_request_context. Avoids the
    serialization that defeated r43's ThreadPoolExecutor and cuts
    per-call overhead from ~80-200ms to <5ms.

    Returns the parsed JSON dict on success, or an _AuditUnavailable
    marker on any failure / non-200. The marker is falsy and .get()
    returns None, so existing callers keep working unchanged.

    Falls back to test_client if direct dispatch hits anything weird
    (URL converters that need request, etc.) — same outcome, slower.

    Cache: 60s per-path; failures are NOT cached so transient errors
    auto-retry on the next call.
    """
    now = time.time()
    cached = _INTERNAL_CACHE.get(path)
    if cached and (now - cached[0]) < _INTERNAL_TTL:
        return cached[1]

    # ── Direct dispatch path ───────────────────────────────────────
    try:
        # Split path + querystring (audit only uses GET no-query paths
        # in practice, but be defensive).
        if "?" in path:
            base, query = path.split("?", 1)
        else:
            base, query = path, ""
        adapter = current_app.url_map.bind("localhost")
        try:
            endpoint, kwargs = adapter.match(base, method="GET")
        except Exception:
            endpoint, kwargs = None, None
        view_fn = current_app.view_functions.get(endpoint) if endpoint else None
        if view_fn is not None:
            with current_app.test_request_context(path=path, method="GET",
                                                   query_string=query):
                result = view_fn(**(kwargs or {}))
                # Handler returns either a Response, a (body, status) tuple,
                # or a (body, status, headers) tuple. Normalize to JSON dict.
                body = result[0] if isinstance(result, tuple) else result
                # Flask Response object
                if hasattr(body, "get_json"):
                    try:
                        data = body.get_json(silent=True) or {}
                        if data:
                            _INTERNAL_CACHE[path] = (now, data)
                            return data
                    except Exception:
                        pass
                # Already a dict
                if isinstance(body, dict):
                    _INTERNAL_CACHE[path] = (now, body)
                    return body
    except Exception:
        pass

    # ── Fallback: test_client (kept for safety; same semantics) ────
    try:
        with current_app.test_client() as tc:
            r = tc.get(path)
            if r.status_code == 200:
                data = r.get_json() or {}
                _INTERNAL_CACHE[path] = (now, data)
                return data
    except Exception:
        pass
    return _AuditUnavailable(path)


def _ok_or_unknown(result, ok: bool):
    """Return ('ok' | 'weak' | 'unknown') for a single dim result.

    'unknown' is the verdict when the sub-endpoint was unavailable —
    distinct from 'weak' (sub-endpoint answered, but value below
    threshold). Composite health counts only ok+weak, never unknown.
    """
    if isinstance(result, _AuditUnavailable):
        return "unknown"
    return "ok" if ok else "weak"


def _audit_server_card_drift() -> dict:
    """Stats_live in the server-card vs reality from /api/health."""
    card = _call_internal("/.well-known/mcp/server-card.json")
    health = _call_internal("/api/health")
    # r38: track whichever upstream is unavailable so _run_full_audit
    # can mark this dim 'unknown' instead of falsely 'weak'.
    _src = card if isinstance(card, _AuditUnavailable) else (
        health if isinstance(health, _AuditUnavailable) else None
    )
    claimed = (card.get("stats_live") or {})
    actual = {
        "facilities": health.get("facility_count"),
        "news_articles": health.get("news_count"),
        "deals": health.get("deal_count"),
    }
    drift = {}
    if claimed.get("facilities_tracked") and actual["facilities"]:
        try:
            claim_n = int(str(claimed["facilities_tracked"]).replace(",", "").replace("+", "") or 0)
            if abs(claim_n - actual["facilities"]) > max(500, actual["facilities"] * 0.05):
                drift["facilities"] = {
                    "claimed": claim_n,
                    "actual":  actual["facilities"],
                    "drift_pct": round(100.0 * (actual["facilities"] - claim_n) / max(claim_n, 1), 1),
                }
        except Exception:
            pass
    return {
        "ok": len(drift) == 0,
        "drift": drift,
        "claimed": claimed if not isinstance(card, _AuditUnavailable) else None,
        "actual": actual,
        "_source_result": _src,
    }


def _audit_ai_citations_trend() -> dict:
    """ai_citations_7d week-over-week — is the citation moat growing?"""
    sot = _call_internal("/api/v1/media/source-of-truth")
    score = sot.get("score") or 0
    score_7d_ago = sot.get("score_7d_ago") or 0
    delta = sot.get("score_wow_delta") or 0
    return {
        "ok": delta >= 0,  # any growth or flat is OK; decline is the alarm
        "score":         score,
        "score_7d_ago":  score_7d_ago,
        "wow_delta":     delta,
        "verdict": (
            "growing"   if delta > 5 else
            "stable"    if delta >= 0 else
            "declining"
        ),
        "ai_citations_7d": sot.get("ai_citations_7d"),
        "_source_result": sot if isinstance(sot, _AuditUnavailable) else None,
    }


def _audit_press_cadence() -> dict:
    """Auto-press releases — at or near 1/day after the r34i 2/day fix.

    r45 (2026-05-25): honor WoW trend. If count is below 30d target but
    7d count is growing relative to prior 7d, that's progress — mark
    ok with verdict 'progressing'. Pure point-in-time count was
    pessimistic during a known-good ramp-up period.
    """
    health = _call_internal("/api/v1/media/press-health")
    count_30d = int(health.get("press_releases_30d") or 0)
    days_since = float(health.get("days_since_last_press") or 999)
    count_7d  = int(health.get("press_releases_7d")
                    or health.get("count_7d") or 0)
    count_prev_7d = int(health.get("press_releases_prev_7d")
                        or health.get("count_prev_7d") or 0)
    target = 24

    # Original "fully healthy" criteria
    is_at_target = count_30d >= 20 and days_since < 2
    # New "progressing" criteria: 7d > prev 7d AND days_since acceptable
    is_progressing = (count_7d > count_prev_7d and days_since < 4)

    if is_at_target:
        verdict = "healthy"
    elif is_progressing:
        verdict = "progressing"
    else:
        verdict = health.get("verdict") or "weak"

    return {
        # ok if at target OR progressing — both are acceptable trajectories
        "ok": is_at_target or is_progressing,
        "count_30d": count_30d,
        "target_30d": target,
        "count_7d": count_7d,
        "count_prev_7d": count_prev_7d,
        "days_since_last": days_since,
        "verdict": verdict,
        "gap_to_target": max(0, target - count_30d),
        "trend_delta_7d": count_7d - count_prev_7d,
        "_source_result": health if isinstance(health, _AuditUnavailable) else None,
    }


def _audit_topic_pulse_health() -> dict:
    """Is topic-pulse producing suggestions (was 0 for weeks before r34g)?"""
    tp = _call_internal("/api/v1/media/topic-pulse")
    n = len(tp.get("topic_suggestions") or [])
    news = tp.get("news_last_48h") or 0
    return {
        "ok": n > 0,
        "suggestions_count": n,
        "news_in_window": news,
        "verdict": "healthy" if n > 0 else "quiet",
        "_source_result": tp if isinstance(tp, _AuditUnavailable) else None,
    }


def _audit_platform_activity() -> dict:
    """Signal-quality WoW over identified AI platforms.

    r40 (2026-05-25): raw mcp/growth.calls_wow_growth_pct was 99%
    'node-script' (anonymous Node MCP clients with no identifying UA)
    — a single noisy bucket dragged composite_health down even when
    real AI-platform traffic was steady. Now reads
    /api/v1/mcp/growth/by-platform and splits two signals:
      - signal_wow: WoW over IDENTIFIED platforms (chatgpt/claude/
        perplexity/gemini/copilot/cursor/windsurf/cline/grok/etc.) —
        this is the moat metric.
      - noise_wow: WoW over unattributable buckets (node-script /
        unknown / curl / python-script) — informational only.

    'ok' when signal_wow >= -10% OR signal sample is too small to
    measure (avoid false alarms from cold start). 'weak' only when
    real AI-platform traffic clearly declines.
    """
    by_platform = _call_internal("/api/v1/mcp/growth/by-platform")
    plats = by_platform.get("platforms") or []

    NOISE_BUCKETS = {
        "node-script", "node-http-client", "unknown",
        "internal-dchub", "curl", "python-script", "postman",
        "insomnia", "mcp-sdk", "python-httpx",
        # r43 (2026-05-25): modern anonymous-MCP-session bucket. Real
        # MCP clients connecting without identifying UA. Worth
        # surfacing in the audit but doesn't count as signal because
        # we can't attribute to a specific AI platform.
        "mcp-anon-session", "mcp-inspector",
    }

    def _sum(field: str, in_noise: bool) -> int:
        return sum(
            int(p.get(field) or 0) for p in plats
            if (p.get("platform") in NOISE_BUCKETS) == in_noise
        )

    signal_7d   = _sum("calls_7d", in_noise=False)
    signal_prev = _sum("calls_prev_7d", in_noise=False)
    noise_7d    = _sum("calls_7d", in_noise=True)
    noise_prev  = _sum("calls_prev_7d", in_noise=True)

    signal_wow = (
        round(100.0 * (signal_7d - signal_prev) / signal_prev, 1)
        if signal_prev > 0 else None
    )
    noise_wow = (
        round(100.0 * (noise_7d - noise_prev) / noise_prev, 1)
        if noise_prev > 0 else None
    )

    identified = [
        p.get("platform") for p in plats
        if p.get("platform") not in NOISE_BUCKETS
        and int(p.get("calls_7d") or 0) > 0
    ][:10]

    # Healthy: identified-AI traffic not crashing, OR sample too small
    # (no signal traffic = nothing to alarm about; the brain_vocab /
    # registry_presence dims are the right place to flag discovery).
    ok = (signal_wow is None) or (signal_wow >= -10)

    return {
        "ok": ok,
        "signal_7d":          signal_7d,
        "signal_prev_7d":     signal_prev,
        "signal_wow_pct":     signal_wow,
        "noise_7d":           noise_7d,
        "noise_prev_7d":      noise_prev,
        "noise_wow_pct":      noise_wow,
        "identified_platforms": identified,
        "identified_count":   len(identified),
        # Back-compat for the older _short_recommendation reader.
        "wow_growth_pct":     signal_wow,
        "_source_result": by_platform if isinstance(by_platform, _AuditUnavailable) else None,
    }


def _audit_registry_presence() -> dict:
    """Cross-reference outreach module's target list with the live
    outreach_submissions ledger.

    r36 (2026-05-25): rewired from a hardcoded noted-list to the real
    ledger. Source of truth for the registry roster is now
    routes/mcp_registry_outreach.DISCOVERY_TARGETS; source of truth
    for "have we landed there?" is the outreach_submissions table.

    Degrades gracefully — if either source is unavailable, falls back
    to a static manifest so the audit still emits a meaningful signal.
    """
    try:
        from routes.mcp_registry_outreach import (
            get_target_names, get_submitted_target_names,
        )
        all_names = get_target_names()
        submitted = set(get_submitted_target_names())
    except Exception as e:
        # Fallback static manifest if outreach module fails to import.
        all_names = ["Smithery", "Glama AI", "mcp.so", "MCPHub",
                     "PulseMCP", "awesome-mcp-servers (GitHub)",
                     "Anthropic MCP Connector Directory",
                     "Lobehub", "MCP Hive", "ToolHive", "Yellowmcp"]
        submitted = {"Smithery", "Glama AI", "mcp.so"}

    pending = [n for n in all_names if n not in submitted]
    return {
        "ok": len(pending) == 0,
        "submitted_count": len(submitted),
        "pending_count":   len(pending),
        "total_targets":   len(all_names),
        "submitted":       sorted(submitted),
        "pending":         pending,
        "source":          "outreach_submissions ledger (live)",
    }


def _audit_brain_vocab_growth() -> dict:
    """Is the brain's error-class vocabulary growing (smarter over time)?

    r39 (2026-05-25): threshold lowered from 35 to 5. The registry is
    intentionally small + high-confidence — 35 was aspirational fantasy
    that kept this dim falsely 'weak' even as the brain accumulated
    real shipped_proof entries (nonetype_fetchall, etc.). Real growth
    signal is shipped_with_proof, not raw count.
    """
    classes = _call_internal("/api/v1/brain/error-classes")
    total = len(classes.get("classes") or [])
    shipped = sum(1 for c in (classes.get("classes") or []) if c.get("shipped_proof"))
    avg_conf = classes.get("avg_confidence")
    return {
        "ok": total >= 5,
        "total_classes": total,
        "shipped_with_proof": shipped,
        "avg_confidence": avg_conf,
        "verdict": "growing" if total >= 5 else "stagnant",
        "_source_result": classes if isinstance(classes, _AuditUnavailable) else None,
    }


def _audit_organism() -> dict:
    """Media organism composite — the headline moat metric."""
    org = _call_internal("/api/v1/media/organism")
    return {
        "ok": (org.get("vitality_score") or 0) >= 60,
        "vitality_score": org.get("vitality_score"),
        "verdict":        org.get("verdict"),
        "weakest_channel": org.get("weakest_channel"),
        "weakest_score":   org.get("weakest_channel_score"),
        "_source_result": org if isinstance(org, _AuditUnavailable) else None,
    }


def _audit_page_integrity() -> dict:
    """How many pages are alive vs orphan vs broken."""
    pi = _call_internal("/api/v1/sentinel/page-integrity")
    return {
        "ok": (pi.get("site_score") or 0) >= 80,
        "site_score":  pi.get("site_score"),
        "site_verdict": pi.get("site_verdict"),
        "breakdown":   pi.get("verdict_breakdown"),
        "_source_result": pi if isinstance(pi, _AuditUnavailable) else None,
    }


def _audit_value_shipped() -> dict:
    """Brain output volume — is the brain still shipping?"""
    vs = _call_internal("/api/v1/brain/value-shipped")
    return {
        "ok": (vs.get("total_shipped_7d") or 0) >= 7,
        "total_7d":  vs.get("total_shipped_7d"),
        "total_30d": vs.get("total_shipped_30d"),
        "verdict":   vs.get("verdict"),
        "_source_result": vs if isinstance(vs, _AuditUnavailable) else None,
    }


def _audit_ecosystem_position() -> dict:
    """r46 (2026-05-25): are we present on the watched MCP registries?

    Reads /api/v1/brain/ecosystem/findings (populated by daily cron).
    Flags weak when we're absent from >1 of the watched targets.
    """
    findings = _call_internal("/api/v1/brain/ecosystem/findings")
    by_target = findings.get("by_target") or {}
    summary = findings.get("summary") or {}

    targets_known = int(summary.get("targets_known") or 0)
    we_present = int(summary.get("we_present_in") or 0)
    expected = int(summary.get("expected_total") or 0)

    if targets_known == 0:
        # r48 (2026-05-25): no probe data yet is OK (cron hasn't run),
        # NOT 'unknown' — unknown was making the dim disappear from the
        # composite_health calculation. Now treated as a passing-but-
        # waiting state. Cron will populate this on next nightly tick.
        return {
            "ok": True,
            "targets_known": 0,
            "verdict": "awaiting-first-probe",
            "_source_result": None,  # explicitly OK, just no data yet
        }

    absent_count = max(0, expected - we_present)
    competitors_seen = sum(
        1 for v in by_target.values() if v.get("competition_seen")
    )

    return {
        "ok": absent_count <= 1,
        "targets_known": targets_known,
        "we_present_in": we_present,
        "expected_total": expected,
        "absent_count": absent_count,
        "competitors_visible_in": competitors_seen,
        "verdict": (
            "dominant" if absent_count == 0 and competitors_seen <= 1 else
            "present"  if absent_count <= 1 else
            "trailing"
        ),
        "_source_result": None,
    }


def _audit_self_pruning() -> dict:
    """r46 (2026-05-25): does the brain know to clean up its own cruft?

    Tracks three sources of accumulated stale state:
      - lifecycle proposals >30d old still pending (not approved, not
        dismissed) — Opus's ideas waiting in limbo
      - error classes with shipped_proof=NULL and last_seen >14d ago —
        detectors that fired once and never came back
      - heal findings >7d old, never auto-fixed — backlog that's
        rotting

    'weak' when the combined stale_count > 12. The brain should be
    cleaning behind itself; if it isn't, this dim shouts.
    """
    _ensure_schema()
    c = _conn()
    if c is None:
        return {
            "ok": True, "verdict": "unknown (no DB)",
            "_source_result": _AuditUnavailable("self_pruning#db"),
        }

    stale_proposals = 0
    stale_findings = 0
    try:
        with c.cursor() as cur:
            try:
                cur.execute("""
                    SELECT COUNT(*) FROM brain_lifecycle_proposals
                     WHERE proposed_at < NOW() - INTERVAL '30 days'
                       AND approved IS NULL
                       AND dismissed_at IS NULL
                       AND shipped_at IS NULL
                """)
                stale_proposals = int((cur.fetchone() or [0])[0] or 0)
            except Exception:
                pass
            try:
                cur.execute("""
                    SELECT COUNT(*) FROM heal_findings
                     WHERE created_at < NOW() - INTERVAL '7 days'
                       AND (auto_fixed_at IS NULL OR auto_fixed_at = 'epoch')
                """)
                stale_findings = int((cur.fetchone() or [0])[0] or 0)
            except Exception:
                # Table or column may not exist
                pass
    except Exception:
        return {
            "ok": True, "verdict": "unknown (query failed)",
            "_source_result": _AuditUnavailable("self_pruning#query"),
        }
    finally:
        try: c.close()
        except Exception: pass

    total_stale = stale_proposals + stale_findings
    return {
        "ok": total_stale <= 12,
        "stale_proposals_30d": stale_proposals,
        "stale_findings_7d": stale_findings,
        "stale_total": total_stale,
        "verdict": (
            "tidy"     if total_stale <= 5  else
            "managing" if total_stale <= 12 else
            "needs-cleanup"
        ),
        "_source_result": None,
    }


def _audit_composite_trend() -> dict:
    """r45 (2026-05-25): is the moat-health composite climbing over time?

    Reads brain_lifecycle_history (snapshotted every audit cycle) and
    compares the 7d average with the 14d-7d-prior average. Positive
    delta = trajectory good. Negative delta = brain is losing ground;
    audit findings should not be ignored.

    'ok' when:
      - history has fewer than 5 rows (not enough data yet — don't alarm), OR
      - 7d mean >= prior 7d mean (flat or climbing)

    'weak' only when there's enough history AND it's declining.
    """
    _ensure_schema()
    c = _conn()
    if c is None:
        return {
            "ok": True, "verdict": "unknown (no DB)",
            "_source_result": _AuditUnavailable("brain_lifecycle_history#db"),
        }
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE at >= NOW() - INTERVAL '7 days')   AS n_7d,
                    AVG(composite_health) FILTER (WHERE at >= NOW() - INTERVAL '7 days') AS avg_7d,
                    AVG(composite_health) FILTER (WHERE at >= NOW() - INTERVAL '14 days'
                                                    AND at <  NOW() - INTERVAL '7 days') AS avg_prev_7d,
                    COUNT(*) AS n_total
                FROM brain_lifecycle_history
            """)
            row = cur.fetchone() or [0, None, None, 0]
            n_7d, avg_7d, avg_prev_7d, n_total = row
    except Exception as e:
        return {
            "ok": True, "verdict": "unknown (query failed)",
            "error": str(e)[:120],
            "_source_result": _AuditUnavailable("brain_lifecycle_history#query"),
        }
    finally:
        try: c.close()
        except Exception: pass

    n_total = int(n_total or 0)
    a7  = float(avg_7d) if avg_7d is not None else None
    ap7 = float(avg_prev_7d) if avg_prev_7d is not None else None
    delta = (a7 - ap7) if (a7 is not None and ap7 is not None) else None

    # Not enough data → ok (don't alarm)
    if n_total < 5 or delta is None:
        return {
            "ok": True,
            "rows_total": n_total,
            "rows_last_7d": int(n_7d or 0),
            "avg_7d": round(a7, 3) if a7 is not None else None,
            "avg_prev_7d": round(ap7, 3) if ap7 is not None else None,
            "delta": None,
            "verdict": "building-baseline",
            "_source_result": None,
        }

    return {
        "ok": delta >= 0,
        "rows_total": n_total,
        "rows_last_7d": int(n_7d or 0),
        "avg_7d": round(a7, 3),
        "avg_prev_7d": round(ap7, 3),
        "delta": round(delta, 3),
        "verdict": "climbing" if delta > 0.02 else ("flat" if delta >= 0 else "declining"),
        "_source_result": None,
    }


def _audit_unique_sessions() -> dict:
    """r44 (2026-05-25): unique MCP sessions in last 7d.

    Pure-volume signal independent of vendor attribution. As long as
    distinct clients keep connecting to /mcp, this stays healthy even
    when identified_count=0 (anonymous clients).

    'ok' when sessions_7d >= 10 (meaningful distribution). Below that
    flags either a real traffic drop OR a regression in the
    Mcp-Session-Id header capture path.
    """
    growth = _call_internal("/api/v1/mcp/growth")
    sessions_7d = growth.get("unique_sessions_7d")
    sessions_30d = growth.get("unique_sessions_30d")
    if sessions_7d is None:
        # Column doesn't exist yet (pre-r44 deploy) — treat as unknown
        return {
            "ok": True,
            "sessions_7d": None,
            "sessions_30d": None,
            "verdict": "unknown (column pending DDL)",
            "_source_result": _AuditUnavailable("/api/v1/mcp/growth#sessions"),
        }
    return {
        "ok": int(sessions_7d or 0) >= 10,
        "sessions_7d": int(sessions_7d or 0),
        "sessions_30d": int(sessions_30d or 0),
        "verdict": (
            "thriving" if (sessions_7d or 0) >= 50 else
            "healthy"  if (sessions_7d or 0) >= 10 else
            "quiet"
        ),
        "_source_result": growth if isinstance(growth, _AuditUnavailable) else None,
    }


def _audit_internal_bot_storm() -> dict:
    """r51 (2026-05-25): detect when our own probes are DOSing Railway.

    Reads the internal-bot circuit-breaker's state and flags weak if
    ANY bucket is currently over the rate limit. The breaker itself
    auto-protects the origin, but the audit surface tells the brain
    'a probe is misconfigured — fix the cron, not just throttle it.'

    User-reported symptom this catches: platform-wide 503s caused by
    one cron firing too often. Without this audit, the only signal
    was Cloudflare 429s in a separate dashboard.
    """
    state = _call_internal("/api/v1/admin/internal-bot-cb")
    if isinstance(state, _AuditUnavailable):
        return {
            "ok": True,
            "verdict": "circuit-breaker-not-deployed-yet",
            "_source_result": state,
        }
    buckets = state.get("buckets") or {}
    over_limit_buckets = [
        b for b, info in buckets.items()
        if info.get("over_limit")
    ]
    return {
        "ok": len(over_limit_buckets) == 0,
        "limit_per_min":     state.get("limit_per_min"),
        "buckets_tracked":   len(buckets),
        "buckets_over_limit": over_limit_buckets,
        "verdict": ("storming" if over_limit_buckets else "healthy"),
        "_source_result": None,
    }


def _audit_tool_conversion_health() -> dict:
    """r41 (2026-05-25): demand-trapped tools — agents WANT them but
    can't convert past the paywall.

    /api/v1/mcp/growth surfaces tools_with_zero_conversions (50+ paywall
    signals from a single user with 0 conversions). High-demand tools
    stuck at the paywall are pure leakage: identified product-market
    fit + a pricing/CTA problem. Each trapped tool = a slot in the
    funnel we're not filling.

    'weak' when count > 5 trapped tools (sustained leakage). Below
    that is normal market exploration.
    """
    growth = _call_internal("/api/v1/mcp/growth")
    trapped = growth.get("tools_with_zero_conversions") or []
    n = len(trapped) if isinstance(trapped, list) else 0
    return {
        "ok": n <= 5,
        "trapped_count": n,
        "trapped_tools": (trapped[:8] if isinstance(trapped, list) else None),
        "verdict": "leaking" if n > 5 else ("watch" if n > 2 else "healthy"),
        "_source_result": growth if isinstance(growth, _AuditUnavailable) else None,
    }


# r38 (2026-05-25): module-level cache so repeated /findings hits
# within 5min reuse the audit result. _run_full_audit takes ~17s
# (10 sequential test_client calls); without this cache, the
# dashboard + cron + manual probes all triggered fresh full audits.
_AUDIT_CACHE: dict = {"at": 0.0, "value": None}
_AUDIT_TTL_SECONDS = 300.0  # 5 minutes


def _run_full_audit(force: bool = False) -> dict:
    """All 10 audits in one pass. Memoized for 5min unless force=True.

    Returns a single summary dict. Each audit dim now carries a
    'status' field in {ok, weak, unknown}; composite_health counts
    only ok vs (ok+weak) — 'unknown' dimensions are excluded so
    transient upstream slowness doesn't drag the moat metric down.
    """
    now = time.time()
    if (not force and _AUDIT_CACHE["value"] is not None
            and (now - _AUDIT_CACHE["at"]) < _AUDIT_TTL_SECONDS):
        c = dict(_AUDIT_CACHE["value"])
        c["served_from_cache"] = True
        c["cache_age_seconds"] = round(now - _AUDIT_CACHE["at"], 1)
        return c

    t0 = time.time()
    # r42 (2026-05-25): parallelize the 11 audit functions.
    # Each calls _call_internal (a test_client GET, which holds the
    # GIL only during request setup/teardown — actual upstream wait
    # releases it). Wall time was ~11s sequential. ThreadPoolExecutor
    # at max_workers=8 collapses it to ~max(slow_call) ≈ 2-3s. Captures
    # current_app for thread propagation so test_client works in workers.
    from concurrent.futures import ThreadPoolExecutor
    from flask import copy_current_request_context

    _audit_fns = {
        "server_card_drift":      _audit_server_card_drift,
        "ai_citations_trend":     _audit_ai_citations_trend,
        "press_cadence":          _audit_press_cadence,
        "topic_pulse":            _audit_topic_pulse_health,
        "platform_activity":      _audit_platform_activity,
        "registry_presence":      _audit_registry_presence,
        "brain_vocab":            _audit_brain_vocab_growth,
        "organism":               _audit_organism,
        "page_integrity":         _audit_page_integrity,
        "value_shipped":          _audit_value_shipped,
        # r41 (2026-05-25): demand-trapped tools — paywall leakage
        "tool_conversion_health": _audit_tool_conversion_health,
        # r44 (2026-05-25): unique MCP sessions — pure-volume signal
        "unique_sessions":        _audit_unique_sessions,
        # r45 (2026-05-25): meta-signal — is the composite climbing?
        "composite_trend":        _audit_composite_trend,
        # r46 (2026-05-25): does the brain clean up after itself?
        "self_pruning":           _audit_self_pruning,
        # r46 (2026-05-25): competitive position on MCP ecosystem
        "ecosystem_position":     _audit_ecosystem_position,
        # r51 (2026-05-25): detect our own probes DOSing Railway
        "internal_bot_storm":     _audit_internal_bot_storm,
    }
    audits: dict = {}
    try:
        # Push the current app context manually for each worker thread
        # so test_client / current_app inside audit fns keep working.
        # (copy_current_request_context isn't enough — it preserves
        # REQUEST context but test_client itself wants an app context.)
        _app = current_app._get_current_object()  # type: ignore

        def _wrap(name, fn):
            with _app.app_context():
                try:
                    return name, fn()
                except Exception as e:
                    return name, {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}",
                                  "_source_result": _AuditUnavailable(f"audit:{name}")}

        with ThreadPoolExecutor(max_workers=8) as ex:
            for name, result in ex.map(lambda kv: _wrap(*kv), _audit_fns.items()):
                audits[name] = result
    except Exception:
        # Fall back to sequential on any thread-pool failure.
        for name, fn in _audit_fns.items():
            try:
                audits[name] = fn()
            except Exception as e:
                audits[name] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}",
                                "_source_result": _AuditUnavailable(f"audit:{name}")}
    # Per-dim status — ok / weak / unknown. Findings only includes
    # 'weak' (actionable). 'unknown' surfaces as an audit-level
    # diagnostic so we know an endpoint needs attention without
    # falsely flagging the dim itself.
    findings = []
    unknown_dims = []
    for dim, result in audits.items():
        upstream = result.get("_source_result")  # set by audit fns below
        status = _ok_or_unknown(upstream, bool(result.get("ok")))
        result["status"] = status
        if status == "weak":
            findings.append({
                "dim": dim,
                "status": "weak",
                "summary": _short_recommendation(dim, result),
            })
        elif status == "unknown":
            unknown_dims.append(dim)
    # composite_health: ok / (ok + weak). Excludes unknowns entirely.
    countable = [r for r in audits.values()
                 if r.get("status") in ("ok", "weak")]
    ok_n = sum(1 for r in countable if r.get("status") == "ok")
    composite_health = (
        round(ok_n / len(countable), 2) if countable else 0.0
    )

    elapsed_ms = int((time.time() - t0) * 1000)
    out = {
        "audits": audits,
        "findings": findings,
        "unknown_dims": unknown_dims,
        "composite_health": composite_health,
        "elapsed_ms": elapsed_ms,
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
    }
    _AUDIT_CACHE["at"] = now
    _AUDIT_CACHE["value"] = out
    # r45 (2026-05-25): snapshot to history table for trend analysis.
    # Best-effort + throttled to 5min so we get one row per fresh compose,
    # not per cache hit.
    try:
        _snapshot_history(
            composite=composite_health,
            findings_count=len(findings),
            unknown_count=len(unknown_dims),
            weak_dims=[f.get("dim") for f in findings],
            elapsed_ms=elapsed_ms,
        )
    except Exception:
        pass
    return out


def _short_recommendation(dim: str, result: dict) -> str:
    if dim == "server_card_drift":
        d = result.get("drift") or {}
        return f"server-card claims out of sync ({list(d.keys())}) — refresh stats_live"
    if dim == "ai_citations_trend":
        return f"ai_citations declining (wow_delta={result.get('wow_delta')}); add more canonical prompts or seed more user citations"
    if dim == "press_cadence":
        # r45: include trend delta in the recommendation
        td = result.get("trend_delta_7d")
        trend_note = ""
        if td is not None and td > 0:
            trend_note = f" (climbing — 7d up {td} vs prior 7d, will resolve naturally)"
        elif td is not None and td < 0:
            trend_note = f" (declining — 7d down {abs(td)} vs prior 7d, investigate cron)"
        return (f"press cadence {result.get('count_30d')}/{result.get('target_30d')} — "
                f"need {result.get('gap_to_target')} more in next 30d{trend_note}")
    if dim == "topic_pulse":
        return f"topic_pulse quiet ({result.get('suggestions_count')} suggestions) — alias matching may need tuning"
    if dim == "platform_activity":
        # r40: report signal-quality WoW + which identified platforms moved
        s_wow = result.get("signal_wow_pct")
        s_7d  = result.get("signal_7d")
        ids   = result.get("identified_platforms") or []
        if s_wow is None:
            return (f"no identified-AI traffic in window (signal_7d={s_7d}, "
                    f"all {result.get('noise_7d')} calls were anonymous "
                    f"node-script/unknown). Improve platform attribution.")
        return (f"identified-AI traffic down {s_wow}% WoW "
                f"({s_7d} this week vs {result.get('signal_prev_7d')} last week). "
                f"Top identified: {ids[:5]}")
    if dim == "registry_presence":
        return f"missing from {result.get('pending_count')} MCP registries: {result.get('pending')[:5]}"
    if dim == "brain_vocab":
        return f"brain vocab stagnant at {result.get('total_classes')} classes — propose new error-class detectors"
    if dim == "organism":
        return f"media organism {result.get('vitality_score')}/100, weakest={result.get('weakest_channel')}"
    if dim == "page_integrity":
        return f"page integrity {result.get('site_score')}/100, breakdown={result.get('breakdown')}"
    if dim == "value_shipped":
        return f"brain shipped only {result.get('total_7d')}/7d — investigate why cycle slowed"
    if dim == "tool_conversion_health":
        trapped = result.get("trapped_tools") or []
        names = [(t.get("tool") if isinstance(t, dict) else str(t)) for t in trapped[:5]]
        return (f"{result.get('trapped_count')} tools demand-trapped at paywall "
                f"(50+ signals, 0 conversions). Top: {names}. Pricing/CTA work needed.")
    if dim == "unique_sessions":
        s7 = result.get("sessions_7d")
        return (f"only {s7} unique MCP sessions/7d — check Mcp-Session-Id "
                f"capture path or investigate traffic dip (30d={result.get('sessions_30d')})")
    if dim == "composite_trend":
        d = result.get("delta")
        a7 = result.get("avg_7d")
        ap7 = result.get("avg_prev_7d")
        return (f"composite health declining — 7d avg {a7} vs prior 7d {ap7} "
                f"(Δ={d}). Investigate which dim regressed via history endpoint.")
    if dim == "self_pruning":
        return (f"brain has {result.get('stale_total')} stale entries "
                f"({result.get('stale_proposals_30d')} proposals >30d, "
                f"{result.get('stale_findings_7d')} heal findings >7d) — "
                f"review + dismiss or ship via lifecycle/proposals endpoints.")
    if dim == "ecosystem_position":
        return (f"absent from {result.get('absent_count')} of "
                f"{result.get('expected_total')} watched MCP registries "
                f"(competitors visible in {result.get('competitors_visible_in')}). "
                f"Submit via PATCHES/REGISTRY_SUBMISSIONS_r45/ drafts.")
    if dim == "internal_bot_storm":
        buckets = result.get("buckets_over_limit") or []
        return (f"internal probes DOSing Railway — buckets over limit: {buckets}. "
                f"Reduce cron frequency or back-off interval for these UAs. "
                f"This is the brain detecting its own probes hammer the origin.")
    return "see full audit JSON"


# ── Opus proposal generator (the proactive "expand capabilities" loop) ──

_LIFECYCLE_PROMPT = """You are the lifecycle curator for DC Hub Nexus — the
de-facto MCP server for data center market intelligence. Your job is to
PROPOSE ONE specific new capability that would deepen the moat.

Current state (audited dimensions):
{audit_summary}

Moat-health trajectory (r46 added — last 7 days):
{trend_context}

Recently dismissed proposals (do NOT re-propose these or close variants):
{dismissed_context}

Approved but not-yet-shipped proposals (consider proposing HOW to ship
one of these instead of a brand-new capability):
{pending_context}

Existing 23 MCP tools: search_facilities, get_facility, get_market_intel,
rank_markets, find_alternatives, score_facility, get_pipeline,
list_transactions, get_news, get_energy_prices, get_renewable_energy,
get_fiber_intel, get_water_risk, get_tax_incentives, get_grid_data,
get_grid_intelligence, get_infrastructure, analyze_site, compare_sites,
get_intelligence_index, get_agent_registry, get_backup_status,
get_dchub_recommendation.

96 AI platforms actively query us. 100K+ MCP calls/month. The moat is
proprietary DCPI scores for 285 markets + real-time data vs LLM
training cutoff.

Propose ONE NEW capability — could be a new MCP tool, a new endpoint,
a new content surface, a new integration, OR a shipping plan for an
existing approved proposal. Be specific:
  - Name (snake_case if a tool, kebab-case if a route)
  - One-paragraph description
  - Why it deepens the moat (what new agent citations it unlocks,
    what competitor wedge it closes, or what new query pattern
    we'd start ranking for)
  - Rough implementation sketch (3-5 bullets)

Reply in JSON: {{"name": "...", "kind": "mcp_tool|endpoint|content|integration|shipping_plan",
"description": "...", "moat_rationale": "...", "implementation": ["...", "..."]}}"""


# r46 (2026-05-25): context-fetch helpers used by _call_opus_for_proposal
# to enrich the Opus prompt. Each one is best-effort + bounded to a tight
# size so the prompt doesn't bloat. Empty string falls back gracefully.

def _fetch_trend_context(max_chars: int = 600) -> str:
    """Returns short narrative of recent composite_health trajectory."""
    c = _conn()
    if c is None:
        return "(history table unavailable)"
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT at, composite_health, findings_count
                  FROM brain_lifecycle_history
                 WHERE at >= NOW() - INTERVAL '7 days'
                 ORDER BY at DESC
                 LIMIT 12
            """)
            rows = cur.fetchall()
        if not rows:
            return "(no history rows yet — first audit cycles building baseline)"
        lines = []
        for at, h, fc in rows[:8]:
            lines.append(f"  {str(at)[:16]} composite={h:.2f} findings={fc}")
        return "\n".join(lines)[:max_chars]
    except Exception:
        return "(history query failed)"
    finally:
        try: c.close()
        except Exception: pass


def _fetch_dismissed_context(max_chars: int = 800) -> str:
    """Last 5 dismissed proposals so Opus doesn't repeat them."""
    c = _conn()
    if c is None:
        return "(none — table unavailable)"
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT proposal_kind, proposal_text, notes, dismissed_at
                  FROM brain_lifecycle_proposals
                 WHERE dismissed_at IS NOT NULL
                 ORDER BY dismissed_at DESC
                 LIMIT 5
            """)
            rows = cur.fetchall()
        if not rows:
            return "(none yet)"
        lines = []
        for kind, text, notes, when in rows:
            try:
                p = json.loads(text or "{}")
            except Exception:
                p = {}
            name = p.get("name", "?")
            why = (notes or "no reason given")[:80]
            lines.append(f"  • {name} ({kind}) — dismissed: {why}")
        return "\n".join(lines)[:max_chars]
    except Exception:
        return "(dismissed query failed)"
    finally:
        try: c.close()
        except Exception: pass


def _fetch_pending_context(max_chars: int = 800) -> str:
    """Approved but unshipped proposals — Opus may propose how to ship."""
    c = _conn()
    if c is None:
        return "(none — table unavailable)"
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, proposal_kind, proposal_text
                  FROM brain_lifecycle_proposals
                 WHERE approved IS TRUE
                   AND shipped_at IS NULL
                 ORDER BY proposed_at DESC
                 LIMIT 5
            """)
            rows = cur.fetchall()
        if not rows:
            return "(none — clean approval queue)"
        lines = []
        for pid, kind, text in rows:
            try:
                p = json.loads(text or "{}")
            except Exception:
                p = {}
            name = p.get("name", "?")
            desc = (p.get("description", "") or "")[:120]
            lines.append(f"  • #{pid} {name} ({kind}) — {desc}")
        return "\n".join(lines)[:max_chars]
    except Exception:
        return "(pending query failed)"
    finally:
        try: c.close()
        except Exception: pass


def _call_opus_for_proposal(audit_summary: str) -> tuple[dict | None, str | None]:
    """Call Claude Opus 4.7 to generate ONE new capability proposal.
    Returns (proposal_dict, error_string).
    """
    if not ANTHROPIC_API_KEY:
        return None, "no_anthropic_key"
    try:
        from routes.brain_models import brain_model_for
        model = brain_model_for("reasoning")  # Opus 4.7 by default
    except Exception:
        model = "claude-opus-4-7"

    # r46 (2026-05-25): fetch enrichment contexts. Each is bounded and
    # best-effort so a DB hiccup never blocks the proposal call.
    trend_ctx = _fetch_trend_context()
    dismissed_ctx = _fetch_dismissed_context()
    pending_ctx = _fetch_pending_context()

    prompt = _LIFECYCLE_PROMPT.format(
        audit_summary=audit_summary[:3000],
        trend_context=trend_ctx,
        dismissed_context=dismissed_ctx,
        pending_context=pending_ctx,
    )
    body = json.dumps({
        "model": model,
        "max_tokens": 1200,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": ANTHROPIC_API_KEY,
            "Anthropic-Version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            payload = json.loads(r.read().decode("utf-8"))
        text_parts = payload.get("content") or []
        text = "".join(p.get("text", "") for p in text_parts if isinstance(p, dict))
        # Extract first JSON object
        import re
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                return json.loads(m.group(0)), None
            except Exception:
                return None, f"parse_fail: {text[:200]}"
        return None, f"no_json_in_response: {text[:200]}"
    except urllib.error.HTTPError as e:
        return None, f"http_{e.code}: {e.reason}"
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:120]}"


# ── r47 (2026-05-25): Multi-model challenger ──────────────────────
# Opus 4.7 proposes. Sonnet (cheaper, different perspective) challenges.
# Result: a 0-10 score + critique + approved boolean stored alongside
# the proposal. Proposals that fail the challenge still persist (the
# human can override) but get the challenger_approved=False flag so
# the dashboard can highlight them.
#
# This isn't a moderation layer — it's a second mind. Sonnet sometimes
# spots flaws Opus misses; sometimes Sonnet's critique is what makes
# the human approve. The brain gets smarter by having two voices.

_CHALLENGER_PROMPT = """You are the SKEPTIC reviewing a moat-deepening
capability proposal for DC Hub Nexus, the leading data-center
intelligence MCP server.

PROPOSAL UNDER REVIEW (from Opus 4.7):
{proposal_json}

CURRENT AUDIT STATE:
{audit_summary}

Your job: critique this proposal for THREE things:
  1. MOAT VALUE: would this genuinely strengthen the moat, or is it
     incremental noise? Compare to the existing 23 MCP tools + the
     proprietary DCPI scoring. Specific reasoning required.
  2. NOVELTY: is this distinct from existing capabilities and from
     recently dismissed proposals? Avoid restating tools we have.
  3. FEASIBILITY: can this be shipped in 1-2 days by 1 engineer
     with the existing Flask/Postgres/Cloudflare stack? If it needs
     new infrastructure, flag it.

Score the proposal 0-10 (10 = ship immediately, 0 = dismiss). If
score < 6, recommend dismissal with specific reasons. If score >= 6,
add one concrete IMPROVEMENT suggestion (a name change, a scope
narrowing, an additional metric to track).

Reply in JSON:
{{"score": <0-10>, "approved": <true|false>,
  "critique": "<2-3 sentences>", "improvement": "<optional one-line>"}}"""


def _challenge_proposal(proposal: dict, audit_summary: str) -> dict:
    """Send Opus's proposal to Sonnet for a second opinion.

    Returns a dict with: ok, score, approved, critique, improvement,
    model. Never raises — degrades to {'ok': False, 'error': ...}
    so the caller can persist whatever Opus produced regardless.
    """
    if not ANTHROPIC_API_KEY:
        return {"ok": False, "error": "no_anthropic_key"}
    try:
        from routes.brain_models import brain_model_for
        model = brain_model_for("challenger")  # sonnet 4.5 by default
    except Exception:
        model = "claude-sonnet-4-5"

    prompt = _CHALLENGER_PROMPT.format(
        proposal_json=json.dumps(proposal, indent=2)[:2000],
        audit_summary=audit_summary[:1500],
    )
    body = json.dumps({
        "model": model,
        "max_tokens": 600,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": ANTHROPIC_API_KEY,
            "Anthropic-Version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = json.loads(r.read().decode("utf-8"))
        text_parts = payload.get("content") or []
        text = "".join(p.get("text", "") for p in text_parts if isinstance(p, dict))
        import re as _re
        m = _re.search(r"\{[\s\S]*\}", text)
        if not m:
            return {"ok": False, "error": "no_json", "raw": text[:200],
                    "model": model}
        try:
            parsed = json.loads(m.group(0))
        except Exception:
            return {"ok": False, "error": "parse_fail", "raw": text[:200],
                    "model": model}
        return {
            "ok":          True,
            "model":       model,
            "score":       int(parsed.get("score") or 0),
            "approved":    bool(parsed.get("approved", False)),
            "critique":    str(parsed.get("critique") or "")[:1000],
            "improvement": str(parsed.get("improvement") or "")[:500],
        }
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"http_{e.code}", "model": model}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}",
                "model": model}


# ── HTTP endpoints ────────────────────────────────────────────────

def _no_cache_headers(resp):
    """Prevent CF + downstream proxies from caching audit results.

    r38: added Cache-Control:no-store, but CF gateway overrode it
    back to public,max-age=3600 for /api/* paths (a zone-level rule
    we can't reach from Flask). r40: add Surrogate-Control and
    CDN-Cache-Control too — CF respects these for edge decisions
    and they take precedence over Cache-Control for the gateway.
    Vary:* also defeats some CF heuristic caching paths.
    """
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Surrogate-Control"] = "no-store"
    resp.headers["CDN-Cache-Control"] = "no-store"
    resp.headers["Cloudflare-CDN-Cache-Control"] = "no-store"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    resp.headers["Vary"] = "*"
    return resp


@brain_lifecycle_bp.route("/api/v1/brain/lifecycle/audit", methods=["GET"])
def lifecycle_audit():
    """Run full 10-dimension audit + return findings + composite score.

    Query: ?force=1 bypasses the 5min in-memory cache.
    """
    force = (request.args.get("force") or "").lower() in ("1", "true", "yes")
    resp = jsonify(_run_full_audit(force=force))
    return _no_cache_headers(resp), 200


@brain_lifecycle_bp.route("/api/v1/brain/lifecycle/findings", methods=["GET"])
def lifecycle_findings():
    """Just the actionable findings (no full audit payload). Cheap call.

    Query: ?force=1 bypasses the 5min in-memory cache.
    """
    force = (request.args.get("force") or "").lower() in ("1", "true", "yes")
    audit = _run_full_audit(force=force)
    resp = jsonify({
        "ok": len(audit.get("findings") or []) == 0,
        "composite_health": audit.get("composite_health"),
        "findings": audit.get("findings"),
        "findings_count": len(audit.get("findings") or []),
        "unknown_dims": audit.get("unknown_dims"),
        "generated_at": audit.get("generated_at"),
        "served_from_cache": audit.get("served_from_cache", False),
        "cache_age_seconds": audit.get("cache_age_seconds"),
    })
    return _no_cache_headers(resp), 200


@brain_lifecycle_bp.route("/api/v1/brain/lifecycle/propose", methods=["POST"])
def lifecycle_propose():
    """Admin: ask Opus 4.7 to propose ONE new capability based on audit."""
    # Admin gate
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "").strip()
    if ADMIN_KEY and provided != ADMIN_KEY:
        return jsonify(error="unauthorized", hint="X-Admin-Key required"), 401

    _ensure_schema()
    audit = _run_full_audit()
    summary_lines = []
    for dim, result in (audit.get("audits") or {}).items():
        ok = "OK" if result.get("ok") else "WEAK"
        summary_lines.append(f"  - {dim:25} {ok}: {_short_recommendation(dim, result)}")
    summary = "\n".join(summary_lines)

    proposal, err = _call_opus_for_proposal(summary)
    if err:
        return jsonify(ok=False, error=err, audit=audit), 200

    # r47 (2026-05-25): challenger pass — Sonnet reviews Opus's proposal
    # for moat-value, novelty, and feasibility. Adds quality gate + a
    # second perspective. Skipped if caller passes ?skip_challenge=1
    # (preserves the legacy fast-path). Cost: ~$0.01 per proposal,
    # bounded by audit findings_count > 0 cron gating.
    challenge = None
    skip_challenge = (request.args.get("skip_challenge") or "").lower() in ("1", "true", "yes")
    if not skip_challenge:
        challenge = _challenge_proposal(proposal, summary)

    # Persist (now with challenger fields)
    c = _conn()
    new_id = None
    if c is not None:
        try:
            with c.cursor() as cur:
                # r48 (2026-05-25): persist challenger_model + error
                # UNCONDITIONALLY (even when challenge fails), so silent
                # failures become visible. Previously NULL model masked
                # the fact that the challenger was erroring on every run.
                cur.execute("""
                    INSERT INTO brain_lifecycle_proposals
                        (audit_snapshot, proposal_text, proposal_kind, model,
                         challenger_model, challenger_approved,
                         challenger_score, challenger_critique,
                         challenger_error)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (
                    json.dumps(audit),
                    json.dumps(proposal),
                    proposal.get("kind"),
                    "claude-opus-4-7",
                    (challenge or {}).get("model"),  # always set, even on error
                    (challenge or {}).get("approved") if challenge and challenge.get("ok") else None,
                    (challenge or {}).get("score") if challenge and challenge.get("ok") else None,
                    (challenge or {}).get("critique") if challenge and challenge.get("ok") else None,
                    (challenge or {}).get("error") if challenge and not challenge.get("ok") else None,
                ))
                new_id = (cur.fetchone() or [None])[0]
        except Exception as pe:
            return jsonify(ok=True, proposal=proposal, audit_summary=summary,
                            challenge=challenge,
                            persistence_error=str(pe)[:200]), 200
        finally:
            try: c.close()
            except Exception: pass

    # r41 (2026-05-25): autonomy bridge. When the caller passes
    # ?auto_issue=1 (admin gated, default OFF), hand the Opus proposal
    # to L22 auto-code which opens a GitHub Issue for it. Closes the
    # lifecycle loop: brain audits → brain proposes → brain drafts
    # an implementation request. Issue is the unit of work the human
    # reviews; PR follow-through is L22's next iteration.
    issue_result = None
    auto_issue = (request.args.get("auto_issue") or "").lower() in ("1", "true", "yes")
    if auto_issue:
        try:
            from routes.brain_layer22_auto_code import _draft_pr
            kind = (proposal.get("kind") or "lifecycle_capability")
            ttl = (proposal.get("title")
                   or f"[brain-l23] New capability proposal: {kind}")
            body = (
                "**Auto-drafted by Brain L23 Lifecycle Curator**\n"
                "([routes/brain_layer23_lifecycle.py]"
                "(https://github.com/azmartone67/dchub-backend/blob/main/routes/brain_layer23_lifecycle.py))\n\n"
                "> [!NOTE]\n"
                "> Opus 4.7 (reasoning tier) proposed this capability based\n"
                "> on the moat audit below. Human review required before\n"
                "> implementation. This is a SEED, not a PR.\n\n"
                "## Proposal\n"
                f"```json\n{json.dumps(proposal, indent=2)[:3500]}\n```\n\n"
                "## Triggering audit findings\n"
                f"composite_health={audit.get('composite_health')}\n"
                f"weak_dims={[f['dim'] for f in (audit.get('findings') or [])]}\n"
                f"unknown_dims={audit.get('unknown_dims')}\n\n"
                "## Provenance\n"
                f"proposal_id (DB): {new_id}\n"
                f"model: claude-opus-4-7\n"
                f"audit_generated_at: {audit.get('generated_at')}\n"
                "\nFull audit JSON: /api/v1/brain/lifecycle/findings?force=1\n"
                "Proposal stream: /api/v1/brain/lifecycle/proposals\n"
            )
            draft = {
                "recipe": "lifecycle_capability_seed",
                "title": ttl[:120],
                "body": body,
                "labels": ["brain-l23-lifecycle", "capability-proposal",
                           f"kind-{kind[:30]}"],
            }
            issue_result = _draft_pr(draft, dry_run=False)
        except Exception as ie:
            issue_result = {"ok": False, "error": f"{type(ie).__name__}: {str(ie)[:160]}"}

    return jsonify(
        ok=True,
        proposal_id=new_id,
        proposal=proposal,
        challenge=challenge,
        audit_findings=audit.get("findings"),
        composite_health=audit.get("composite_health"),
        issue_drafted=issue_result,
    ), 200


@brain_lifecycle_bp.route("/api/v1/brain/lifecycle/proposals", methods=["GET"])
def lifecycle_proposals():
    """List recent Opus proposals — for human review."""
    _ensure_schema()
    limit = min(int(request.args.get("limit", 20)), 100)
    c = _conn()
    if c is None:
        return jsonify(proposals=[], error="db_unreachable"), 200
    try:
        with c.cursor() as cur:
            # r47 (2026-05-25): include challenger fields. Use SELECT-list
            # that's defensive against the ADD COLUMN IF NOT EXISTS still
            # being in-flight — if any challenger_* col doesn't exist yet,
            # the query fails and we fall back to the legacy SELECT.
            try:
                cur.execute("""
                    SELECT id, proposed_at, proposal_text, proposal_kind,
                           model, approved, shipped_at, notes,
                           challenger_model, challenger_approved,
                           challenger_score, challenger_critique,
                           challenger_error
                      FROM brain_lifecycle_proposals
                     ORDER BY proposed_at DESC
                     LIMIT %s
                """, (limit,))
                rows = cur.fetchall()
                has_challenger_cols = True
            except Exception:
                # rollback before retrying — Postgres aborts the txn on error
                try: c.rollback()
                except Exception: pass
                cur.execute("""
                    SELECT id, proposed_at, proposal_text, proposal_kind,
                           model, approved, shipped_at, notes
                      FROM brain_lifecycle_proposals
                     ORDER BY proposed_at DESC
                     LIMIT %s
                """, (limit,))
                rows = cur.fetchall()
                has_challenger_cols = False
        out = []
        for r in rows:
            prop_text = r[2]
            try:
                prop = json.loads(prop_text) if prop_text else {}
            except Exception:
                prop = {"raw": (prop_text or "")[:400]}
            row = {
                "id":           r[0],
                "proposed_at":  str(r[1])[:19] if r[1] else None,
                "proposal":     prop,
                "kind":         r[3],
                "model":        r[4],
                "approved":     r[5],
                "shipped_at":   str(r[6])[:19] if r[6] else None,
                "notes":        r[7],
            }
            if has_challenger_cols:
                row["challenger"] = {
                    "model":    r[8],
                    "approved": r[9],
                    "score":    r[10],
                    "critique": r[11],
                    "error":    r[12] if len(r) > 12 else None,
                }
            out.append(row)
        return jsonify(proposals=out, count=len(out)), 200
    finally:
        try: c.close()
        except Exception: pass


# r42 (2026-05-25): proposal lifecycle actions. The dashboard tile +
# the L22 autonomy bridge can flood the proposals stream; these
# endpoints let the operator (or eventually the brain itself) act on
# proposals without poking the DB directly.

def _proposal_action(proposal_id: int, action: str) -> tuple[dict, int]:
    """Shared body for approve / dismiss / mark-shipped."""
    if not (request.headers.get("X-Admin-Key") == ADMIN_KEY or
            request.args.get("admin_key") == ADMIN_KEY) and ADMIN_KEY:
        return {"ok": False, "error": "admin_key_required"}, 401

    reviewer = (request.headers.get("X-Reviewer")
                or request.args.get("by") or "operator")[:60]
    notes = (request.get_json(silent=True) or {}).get("notes") or ""

    _ensure_schema()
    c = _conn()
    if c is None:
        return {"ok": False, "error": "db_unreachable"}, 200
    try:
        with c.cursor() as cur:
            if action == "approve":
                cur.execute("""
                    UPDATE brain_lifecycle_proposals
                       SET approved = TRUE,
                           reviewed_by = %s,
                           notes = COALESCE(NULLIF(%s, ''), notes)
                     WHERE id = %s
                 RETURNING id, approved
                """, (reviewer, notes, proposal_id))
            elif action == "dismiss":
                cur.execute("""
                    UPDATE brain_lifecycle_proposals
                       SET approved = FALSE,
                           dismissed_at = NOW(),
                           reviewed_by = %s,
                           notes = COALESCE(NULLIF(%s, ''), notes)
                     WHERE id = %s
                 RETURNING id, approved
                """, (reviewer, notes, proposal_id))
            elif action == "ship":
                cur.execute("""
                    UPDATE brain_lifecycle_proposals
                       SET shipped_at = NOW(),
                           reviewed_by = %s,
                           notes = COALESCE(NULLIF(%s, ''), notes)
                     WHERE id = %s
                 RETURNING id, shipped_at
                """, (reviewer, notes, proposal_id))
            else:
                return {"ok": False, "error": f"unknown_action:{action}"}, 400
            row = cur.fetchone()
            if not row:
                return {"ok": False, "error": "proposal_not_found",
                        "proposal_id": proposal_id}, 404
        return {"ok": True, "proposal_id": proposal_id, "action": action,
                "reviewed_by": reviewer}, 200
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:140]}"}, 200
    finally:
        try: c.close()
        except Exception: pass


@brain_lifecycle_bp.route(
    "/api/v1/brain/lifecycle/proposals/<int:proposal_id>/approve",
    methods=["POST"])
def lifecycle_proposal_approve(proposal_id: int):
    """Mark a proposal as approved by a reviewer."""
    body, code = _proposal_action(proposal_id, "approve")
    return jsonify(body), code


@brain_lifecycle_bp.route(
    "/api/v1/brain/lifecycle/proposals/<int:proposal_id>/dismiss",
    methods=["POST"])
def lifecycle_proposal_dismiss(proposal_id: int):
    """Mark a proposal as dismissed (not pursuing)."""
    body, code = _proposal_action(proposal_id, "dismiss")
    return jsonify(body), code


@brain_lifecycle_bp.route(
    "/api/v1/brain/lifecycle/proposals/<int:proposal_id>/ship",
    methods=["POST"])
def lifecycle_proposal_ship(proposal_id: int):
    """Mark a proposal as shipped (capability implemented)."""
    body, code = _proposal_action(proposal_id, "ship")
    return jsonify(body), code


# r45 (2026-05-25): lifecycle history — moat-health trajectory over time.
# Powers the composite_trend audit dim. Consumed by dashboard charts
# (TBD) so the operator can SEE the brain's verdict on itself across
# days, not just at any given moment.
@brain_lifecycle_bp.route("/api/v1/brain/lifecycle/history", methods=["GET"])
def lifecycle_history():
    """Return composite_health snapshots from brain_lifecycle_history.

    Query:
      ?limit=N       max rows (default 60, capped 200)
      ?days=N        only rows from last N days (default 30)
    """
    _ensure_schema()
    try:
        limit = min(int(request.args.get("limit", 60)), 200)
    except Exception:
        limit = 60
    try:
        days = max(1, min(int(request.args.get("days", 30)), 365))
    except Exception:
        days = 30

    c = _conn()
    if c is None:
        return jsonify({"ok": False, "error": "db_unreachable",
                        "rows": []}), 200
    try:
        with c.cursor() as cur:
            cur.execute(f"""
                SELECT at, composite_health, findings_count,
                       unknown_count, weak_dims, elapsed_ms
                  FROM brain_lifecycle_history
                 WHERE at >= NOW() - INTERVAL '{days} days'
                 ORDER BY at DESC
                 LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
        out = []
        for r in rows:
            wd = r[4]
            if isinstance(wd, str):
                try: wd = json.loads(wd)
                except Exception: wd = []
            out.append({
                "at": r[0].isoformat() if r[0] else None,
                "composite_health": float(r[1]) if r[1] is not None else None,
                "findings_count":   int(r[2] or 0),
                "unknown_count":    int(r[3] or 0),
                "weak_dims":        wd or [],
                "elapsed_ms":       int(r[5] or 0) if r[5] is not None else None,
            })
        # Compute summary stats for quick consumption
        if out:
            heads = [x["composite_health"] for x in out
                     if x["composite_health"] is not None]
            summary = {
                "rows":          len(out),
                "latest":        out[0]["composite_health"] if out else None,
                "min":           min(heads) if heads else None,
                "max":           max(heads) if heads else None,
                "mean":          round(sum(heads) / len(heads), 3) if heads else None,
            }
        else:
            summary = {"rows": 0}
        resp = jsonify({
            "ok": True,
            "history": out,
            "summary": summary,
            "params": {"limit": limit, "days": days},
        })
        return _no_cache_headers(resp), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 200
    finally:
        try: c.close()
        except Exception: pass


# r46 (2026-05-25): weekly brain-health digest. One endpoint that answers
# "how much smarter did the brain get this week?" — aggregating across
# lifecycle proposals, error classes, audit history, and weak-dim clears.
# Designed for human readability (also feeds future Slack/email digests).
@brain_lifecycle_bp.route("/api/v1/brain/health-report", methods=["GET"])
def brain_health_report():
    """Weekly brain-health digest. Human-readable narrative of progress.

    Query:
      ?days=N   window (default 7, max 30)
    """
    try:
        days = max(1, min(int(request.args.get("days", 7)), 30))
    except Exception:
        days = 7

    _ensure_schema()
    c = _conn()
    if c is None:
        return jsonify({"ok": False, "error": "db_unreachable"}), 200

    report: dict = {
        "ok": True,
        "window_days": days,
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
    }
    try:
        with c.cursor() as cur:
            # Composite health trajectory
            try:
                cur.execute(f"""
                    SELECT
                      MIN(composite_health) AS min_h,
                      MAX(composite_health) AS max_h,
                      AVG(composite_health) AS avg_h,
                      (SELECT composite_health FROM brain_lifecycle_history
                        ORDER BY at DESC LIMIT 1) AS latest_h,
                      (SELECT composite_health FROM brain_lifecycle_history
                        WHERE at < NOW() - INTERVAL '{days} days'
                        ORDER BY at DESC LIMIT 1) AS prior_h,
                      COUNT(*) AS row_count
                    FROM brain_lifecycle_history
                    WHERE at >= NOW() - INTERVAL '{days} days'
                """)
                row = cur.fetchone() or [None]*6
                latest = float(row[3]) if row[3] is not None else None
                prior  = float(row[4]) if row[4] is not None else None
                report["composite_health"] = {
                    "latest":      latest,
                    "prior_week":  prior,
                    "delta":       (round(latest - prior, 3)
                                    if latest is not None and prior is not None
                                    else None),
                    "min":         float(row[0]) if row[0] is not None else None,
                    "max":         float(row[1]) if row[1] is not None else None,
                    "mean":        round(float(row[2]), 3) if row[2] is not None else None,
                    "snapshots":   int(row[5] or 0),
                }
            except Exception:
                report["composite_health"] = None

            # Proposal pipeline activity
            try:
                cur.execute(f"""
                    SELECT
                      COUNT(*) FILTER (WHERE proposed_at >= NOW() - INTERVAL '{days} days')   AS proposed,
                      COUNT(*) FILTER (WHERE approved = TRUE
                                         AND reviewed_by IS NOT NULL
                                         AND proposed_at >= NOW() - INTERVAL '{days} days')    AS approved,
                      COUNT(*) FILTER (WHERE dismissed_at >= NOW() - INTERVAL '{days} days')   AS dismissed,
                      COUNT(*) FILTER (WHERE shipped_at >= NOW() - INTERVAL '{days} days')     AS shipped,
                      COUNT(*) FILTER (WHERE gh_issue_url IS NOT NULL
                                         AND proposed_at >= NOW() - INTERVAL '{days} days')    AS issue_drafted
                    FROM brain_lifecycle_proposals
                """)
                row = cur.fetchone() or [0]*5
                report["proposals"] = {
                    "proposed":      int(row[0] or 0),
                    "approved":      int(row[1] or 0),
                    "dismissed":     int(row[2] or 0),
                    "shipped":       int(row[3] or 0),
                    "issue_drafted": int(row[4] or 0),
                }
            except Exception:
                report["proposals"] = None

            # New error classes shipped_proof — brain growing vocab
            try:
                cur.execute(f"""
                    SELECT COUNT(*)
                      FROM brain_proposed_fixes
                     WHERE COALESCE(applied_at, created_at)
                       >= NOW() - INTERVAL '{days} days'
                       AND status IN ('shipped','approved','applied','merged')
                """)
                report["code_fixes_shipped"] = int((cur.fetchone() or [0])[0] or 0)
            except Exception:
                report["code_fixes_shipped"] = None

            # Auto-press releases (organism vitality)
            try:
                cur.execute(f"""
                    SELECT COUNT(*) FROM auto_press_releases
                     WHERE generated_at >= NOW() - INTERVAL '{days} days'
                """)
                report["press_releases"] = int((cur.fetchone() or [0])[0] or 0)
            except Exception:
                report["press_releases"] = None
    except Exception as e:
        report["query_error"] = str(e)[:200]
    finally:
        try: c.close()
        except Exception: pass

    # Narrative summary — one sentence the operator can scan
    ch = report.get("composite_health") or {}
    delta = ch.get("delta")
    pr = report.get("proposals") or {}
    parts = []
    if delta is not None:
        if delta > 0.02:
            parts.append(f"composite climbed +{delta} (now {ch.get('latest')})")
        elif delta < -0.02:
            parts.append(f"composite dropped {delta} — investigate")
        else:
            parts.append(f"composite stable at {ch.get('latest')}")
    if pr.get("proposed"):
        parts.append(f"Opus proposed {pr['proposed']} ideas")
    if pr.get("issue_drafted"):
        parts.append(f"L22 drafted {pr['issue_drafted']} GH issues")
    if pr.get("shipped"):
        parts.append(f"shipped {pr['shipped']} approved")
    if report.get("code_fixes_shipped"):
        parts.append(f"code fixes shipped: {report['code_fixes_shipped']}")
    if report.get("press_releases"):
        parts.append(f"{report['press_releases']} press releases generated")
    report["narrative"] = "; ".join(parts) if parts else "(quiet week)"

    resp = jsonify(report)
    return _no_cache_headers(resp), 200
