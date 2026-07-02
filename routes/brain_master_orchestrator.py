"""brain_master_orchestrator.py — the "master shell".

ONE entrypoint that ticks the whole brain discovery→action flywheel in
tiered order, so the loop runs as a single coherent cycle instead of five
disconnected crons (autopilot / L15 / L22 / L23 / verifier). Every layer
already exists and carries its own safety gates; this orchestrates them
and returns a unified report of what was auto-fixed, drafted, escalated,
and verified — the missing "fully automatic and agentic" control surface.

Tiering (matches the evolution plan):
  Tier 1 — AUTO-FIX (autonomous): /api/v1/brain/autopilot/run executes the
           whitelisted Tier-A pattern actions (redirects, recomputes,
           freshness refreshes, restarts). Rate-limited + quarantined.
  Tier 2 — AUTO-DRAFT-PR (review before merge): /api/v1/brain/auto-action/run
           (L15 causal→GH issues) + /api/v1/admin/brain/draft-prs/run
           (L22 auto-code draft PRs). NEVER auto-merges.
  Tier 3 — HUMAN-GATED (propose only): /api/v1/brain/lifecycle/audit (L23
           capability proposals) + a digest of money/pricing findings that
           must NOT auto-act (funnel leaks, addressable demand, citations).
  Verify — /api/v1/brain/autopilot/verify-pending confirms prior actions
           actually resolved their findings (feeds quarantine).

Endpoints:
  POST /api/v1/admin/brain/master-tick         — run one full cycle
       ?dry=1                — preview: skip Tier-1 execution + drafting
       ?tiers=1,2,3,verify   — run a subset (default: all)
  GET  /api/v1/admin/brain/master-tick/last    — last cycle's report

Auth: X-Admin-Key (DCHUB_ADMIN_KEY / DCHUB_INTERNAL_KEY). Fail-closed.
Safety: this layer only CALLS the existing run endpoints, so all their
kill-switches, rate-limits, diff-caps and forbidden-path guards still
apply. A master kill switch BRAIN_MASTER_DISABLED=1 halts the whole tick.
It NEVER merges PRs and NEVER acts on Tier-3 (money) findings.
"""
from __future__ import annotations

import os
import json
import time
import datetime as _dt
import urllib.request
import urllib.error

from flask import Blueprint, jsonify, request

brain_master_orchestrator_bp = Blueprint("brain_master_orchestrator", __name__)

_BACKEND_BASE = os.environ.get(
    "DCHUB_BACKEND_BASE",
    "https://dchub-backend-production.up.railway.app",
)


def _admin_key() -> str | None:
    return os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")


def _is_master_disabled() -> bool:
    return str(os.environ.get("BRAIN_MASTER_DISABLED", "")).lower() in ("1", "true", "yes")


# Finding kinds that must NEVER auto-act — money/pricing/positioning calls
# that stay human-gated. The master tick surfaces these as a digest only.
_HUMAN_GATED_PREFIXES = (
    "mcp_funnel_leak",
    "addressable_demand_unconverted",
    "zero_conversion",
    "citation_score_below",
    "billing", "pricing", "tier_",
)


def _call(method: str, path: str, use_admin: bool = True, timeout: int = 90) -> dict:
    """Self-call an existing run endpoint. Mirrors brain_autopilot._execute_action:
    identify via X-DC-Probe so the rate-limiter bypasses us, attach admin key."""
    url = _BACKEND_BASE.rstrip("/") + path
    started = time.time()
    try:
        data = b"{}" if method == "POST" else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("X-DC-Probe", "master-tick")
        req.add_header("User-Agent", "dchub-master-orchestrator/1.0")
        if use_admin:
            ak = _admin_key()
            if ak:
                req.add_header("X-Admin-Key", ak)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(body)
            except Exception:
                parsed = {"_raw": body[:300]}
            return {"ok": True, "http": resp.status, "ms": int((time.time() - started) * 1000), "data": parsed}
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            body = ""
        return {"ok": False, "http": e.code, "error": f"HTTP {e.code}", "body": body,
                "ms": int((time.time() - started) * 1000)}
    except Exception as e:
        return {"ok": False, "http": None, "error": f"{type(e).__name__}: {str(e)[:160]}",
                "ms": int((time.time() - started) * 1000)}


def _summarize(step: str, r: dict) -> dict:
    """Pull a compact, human-meaningful summary out of each layer's response."""
    d = r.get("data") or {}
    s = {"step": step, "ok": r.get("ok"), "http": r.get("http"), "ms": r.get("ms")}
    if r.get("error"):
        s["error"] = r["error"]
    # Best-effort extraction of the fields each layer reports.
    for k in ("executed", "executed_ok", "escalated", "actions_taken", "drafted",
              "prs_drafted", "issues_opened", "verified", "succeeded", "failed",
              "scanned", "proposals", "count", "processed", "skipped"):
        if isinstance(d, dict) and k in d:
            s[k] = d[k]
    if isinstance(d, dict) and isinstance(d.get("actions"), list):
        s["actions_n"] = len(d["actions"])
    return s


def _human_gated_digest() -> dict:
    """Read the public action-queue and bucket out the money/positioning findings
    that the master tick will NEVER auto-act — surfaced for the human."""
    r = _call("GET", "/api/v1/brain/action-queue?cb=master", use_admin=False, timeout=40)
    items = []
    if r.get("ok"):
        for q in (r.get("data") or {}).get("queue", []):
            issue = q.get("issue", "")
            if any(issue.startswith(p) for p in _HUMAN_GATED_PREFIXES):
                items.append({
                    "issue": issue,
                    "seen_count": q.get("seen_count"),
                    "detail": (q.get("detail") or "")[:180],
                })
    return {"count": len(items), "items": items[:12]}


def _ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS brain_master_ticks (
                id        BIGSERIAL PRIMARY KEY,
                ran_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                dry_run   BOOLEAN,
                tiers     TEXT,
                report    JSONB
            )
        """)
    conn.commit()


def _persist(report: dict):
    try:
        import psycopg2
        du = (os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL", "")).strip()
        if not du:
            return
        with psycopg2.connect(du, connect_timeout=8) as conn:
            _ensure_table(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO brain_master_ticks (dry_run, tiers, report) VALUES (%s,%s,%s)",
                    (bool(report.get("dry_run")), ",".join(report.get("tiers_run", [])),
                     json.dumps(report)))
            conn.commit()
    except Exception:
        pass


def _is_admin(req) -> bool:
    expected = _admin_key()
    if not expected:
        return False
    got = (req.headers.get("X-Admin-Key") or req.args.get("admin_key") or "").strip()
    return bool(got) and got == expected


@brain_master_orchestrator_bp.route("/api/v1/admin/brain/master-tick", methods=["POST"])
def master_tick():
    if not _admin_key():
        return jsonify({"error": "admin_endpoint_unconfigured",
                        "hint": "Set DCHUB_ADMIN_KEY on Railway."}), 503
    if not _is_admin(request):
        return jsonify({"error": "unauthorized"}), 401
    if _is_master_disabled():
        return jsonify({"ok": True, "skipped": True,
                        "reason": "BRAIN_MASTER_DISABLED env set"}), 200

    dry = str(request.args.get("dry", "")).lower() in ("1", "true", "yes")
    want = request.args.get("tiers", "1,2,3,verify")
    tiers = {t.strip() for t in want.split(",") if t.strip()}

    report = {
        "ok": True,
        "ran_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "dry_run": dry,
        "tiers_run": [],
        "steps": [],
    }

    # ── Tier 1 — AUTO-FIX (autonomous) ──────────────────────────────
    # Pass dry through to the autopilot so a dry master-tick previews only.
    if "1" in tiers:
        report["tiers_run"].append("1")
        path = "/api/v1/brain/autopilot/run" + ("?dry=1" if dry else "")
        report["steps"].append(_summarize("tier1.autopilot_run", _call("POST", path)))

    # ── Tier 2 — AUTO-DRAFT-PR (review before merge) ────────────────
    if "2" in tiers:
        report["tiers_run"].append("2")
        if dry:
            report["steps"].append({"step": "tier2.skipped_dry", "ok": True})
        else:
            report["steps"].append(_summarize("tier2.l15_auto_action",
                                               _call("POST", "/api/v1/brain/auto-action/run")))
            report["steps"].append(_summarize("tier2.l22_draft_prs",
                                               _call("POST", "/api/v1/admin/brain/draft-prs/run")))
            # 2026-07-01: the CLOSE half of both open loops. L15 files issues
            # and the drafters open PRs, but nothing retired them — the open
            # lists only grew. Direct in-process calls (not HTTP) so the tick
            # works even if the blueprints failed to register. Both are
            # idempotent, capped, and kill-switchable.
            try:
                from routes.brain_issue_janitor import janitor_sweep as _issue_sweep
                _isj = _issue_sweep(dry_run=False) or {}
                report["steps"].append({"step": "tier2.issue_janitor",
                                        "ok": bool(_isj.get("ok")),
                                        "closed": _isj.get("closed_count", 0),
                                        "detail": {k: _isj.get(k) for k in
                                                   ("closed", "errors", "dry_run", "disabled")}})
            except Exception as _isj_e:
                report["steps"].append({"step": "tier2.issue_janitor", "ok": False,
                                        "error": str(_isj_e)[:160]})
            try:
                from routes.brain_pr_opener import expire_stale_draft_prs as _expire
                _exp = _expire(days=7) or {}
                report["steps"].append({"step": "tier2.draft_pr_expire",
                                        "ok": bool(_exp.get("ok")),
                                        "closed": _exp.get("closed_count", 0),
                                        "detail": _exp})
            except Exception as _exp_e:
                report["steps"].append({"step": "tier2.draft_pr_expire", "ok": False,
                                        "error": str(_exp_e)[:160]})

    # ── Tier 3 — HUMAN-GATED (propose only) ─────────────────────────
    if "3" in tiers:
        report["tiers_run"].append("3")
        # L23 capability proposals (seeds, never auto-built).
        report["steps"].append(_summarize("tier3.l23_lifecycle",
                                           _call("GET", "/api/v1/brain/lifecycle/audit")))
        # Effect-unfixable patterns the autopilot keeps failing on: surface them
        # for human re-channeling instead of bounce-looping forever. Propose-only,
        # additive (deduped) findings; dark no-op unless BRAIN_PROMOTE_ON_FAILURE_ENABLED.
        report["steps"].append(_summarize("tier3.promote_on_failure",
                                           _call("POST", "/api/v1/brain/autopilot/promote-on-failure")))
        # #7 L6 forecast→finding bridge (dark unless BRAIN_FORECAST_FINDINGS_ENABLED).
        report["steps"].append(_summarize("tier3.forecast_findings",
                                           _call("GET", "/api/v1/brain/predictions")))
        # #8 strategic-outcome ledger: re-read baselined rec metrics 14/30d later +
        # stamp moved/flat/regressed. Additive ledger UPDATE only; prompt-feedback
        # it powers is gated by BRAIN_STRATEGIC_LEDGER_FEEDBACK_ENABLED in the planner.
        try:
            from routes.brain_strategic_ledger import stamp_strategic_outcomes
            _led = stamp_strategic_outcomes(max_rows=200) or {}
            report["steps"].append({"step": "tier3.strategic_ledger_stamp",
                                    "ok": bool(_led.get("ok", True)),
                                    "stamped": (_led.get("checked_14d") or 0) + (_led.get("checked_30d") or 0),
                                    "detail": _led})
        except Exception as _led_e:
            report["steps"].append({"step": "tier3.strategic_ledger_stamp", "ok": False,
                                    "error": str(_led_e)[:160]})
        # #6 reasoning lane: top leverage-ranked findings → typed candidates
        # (endpoint→digest / code→L22 draft-PR, auto-merge OFF). Dark + ZERO cost
        # unless BRAIN_REASONING_LANE_ENABLED; spends LLM budget when on → skip dry.
        if not dry:
            report["steps"].append(_summarize("tier3.reasoning_lane_drain",
                                               _call("POST", "/api/v1/brain/reasoning-lane/drain")))
        # Money/positioning findings the brain must NOT auto-act on.
        report["human_decisions"] = _human_gated_digest()

    # ── Verify — confirm prior actions resolved their findings ──────
    if "verify" in tiers:
        report["tiers_run"].append("verify")
        report["steps"].append(_summarize("verify.autopilot",
                                           _call("POST", "/api/v1/brain/autopilot/verify-pending")))

    # Roll-up headline.
    report["headline"] = {
        "steps_ok": sum(1 for s in report["steps"] if s.get("ok")),
        "steps_total": len(report["steps"]),
        "human_decisions_pending": (report.get("human_decisions") or {}).get("count", 0),
    }
    _persist(report)
    return jsonify(report), 200


@brain_master_orchestrator_bp.route("/api/v1/admin/brain/master-tick/last", methods=["GET"])
def master_tick_last():
    if not _is_admin(request):
        return jsonify({"error": "unauthorized"}), 401
    try:
        import psycopg2
        import psycopg2.extras
        du = (os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL", "")).strip()
        with psycopg2.connect(du, connect_timeout=8) as conn:
            _ensure_table(conn)
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT id, ran_at, dry_run, tiers, report "
                            "FROM brain_master_ticks ORDER BY id DESC LIMIT 1")
                row = cur.fetchone()
        if not row:
            return jsonify({"ok": True, "last": None, "note": "no master-tick has run yet"}), 200
        return jsonify({"ok": True, "last": row}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}), 200
