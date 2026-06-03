"""Phase r70 (2026-06-03) — Brain SELF-MODEL: the unified self-state the brain
reasons OVER.

Today the brain has a longitudinal scorecard (/api/v1/brain/evolution) and a
point-in-time pulse (/heartbeat) — but no model of WHAT IT IS: its capabilities,
what it has done, what worked, and what it has learned. Genuine self-awareness
requires the brain to be able to consult such a model before it acts/reasons.

This endpoint assembles that document from tables that ALREADY exist (no new
data, no remote fetches), and emits a compact `prompt_digest` string the LLM
layers (L4/L5/L7/L8/L14/L16/L23) prepend to their prompts so they reason WITH
self-knowledge instead of blind.

Design rules (mirroring brain_evolution.py): single short-lived DB connection,
every query fail-soft, cached, read-only, never 5xx.
"""

from __future__ import annotations

import os
import sys
import time as _time
import datetime as _dt
from flask import Blueprint, jsonify, request
import psycopg2
import psycopg2.extras

brain_self_model_bp = Blueprint("brain_self_model", __name__)

# The LLM reasoning layers that exist in code (per the architecture). Whether
# they actually RUN depends on ANTHROPIC_API_KEY being set in Railway — if it's
# absent every one silently no-ops, so the model reports that honestly.
_REASONING_LAYERS = [
    "L4 novel-fix learner", "L5 codegen/PR-opener", "L7 self-evolving detectors",
    "L8 orchestrator", "L14 causal", "L16 self-critique/calibration",
    "L23 lifecycle curator",
]

_RESOLVED_OUTCOMES = (
    "proposed", "approved", "approval_count_incremented",
    "applied", "shipped", "merged",
)


def _conn():
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db:
        return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


def _safe_one(cur, query, default=None):
    try:
        cur.execute(query)
        row = cur.fetchone()
        return row[0] if row and row[0] is not None else default
    except Exception:
        try: cur.connection.rollback()
        except Exception: pass
        return default


def _safe_dual(cur, query, default=(0, 0)):
    try:
        cur.execute(query)
        row = cur.fetchone()
        if not row:
            return default
        return (int(row[0] or 0), int(row[1] or 0))
    except Exception:
        try: cur.connection.rollback()
        except Exception: pass
        return default


def _safe_rows(cur, query, default=None):
    if default is None:
        default = []
    try:
        cur.execute(query)
        return cur.fetchall() or default
    except Exception:
        try: cur.connection.rollback()
        except Exception: pass
        return default


def _capability_counts():
    """Count autopilot patterns (autonomous = has an HTTP method; escalation =
    method None). Lazy import — brain_autopilot is already loaded by the time a
    request lands. Fail-soft → (None, None)."""
    try:
        from routes.brain_autopilot import _PATTERN_LIBRARY
        total = len(_PATTERN_LIBRARY)
        autonomous = sum(1 for v in _PATTERN_LIBRARY.values()
                         if isinstance(v, dict) and v.get("method"))
        return total, autonomous
    except Exception:
        return None, None


def compute_self_model() -> dict:
    out = {"generated_at": _dt.datetime.utcnow().isoformat() + "Z", "ok": True}
    reasoning_online = bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip())
    ap_total, ap_auto = _capability_counts()

    c = _conn()
    if c is None:
        out["ok"] = False
        out["error"] = "db_unavailable"
        return out
    try:
        with c.cursor() as cur:
            active_detectors = _safe_one(cur, """
                SELECT COUNT(DISTINCT issue_label) FROM brain_issue_persistence
                 WHERE last_seen_at >= NOW() - INTERVAL '30 days'
            """, default=None)

            open_findings = _safe_one(cur, """
                SELECT COUNT(*) FROM brain_issue_persistence
                 WHERE last_seen_at >= NOW() - INTERVAL '24 hours'
            """, default=0)

            ok_30d, fail_30d = _safe_dual(cur, """
                SELECT
                  COUNT(*) FILTER (WHERE outcome = 'executed_ok'
                                    AND started_at >= NOW() - INTERVAL '30 days'),
                  COUNT(*) FILTER (WHERE outcome = 'execution_failed'
                                    AND started_at >= NOW() - INTERVAL '30 days')
                  FROM brain_autopilot_actions
            """, default=(0, 0))
            attempted = ok_30d + fail_30d
            fix_success_rate = round(ok_30d / attempted, 3) if attempted else None

            recent_rows = _safe_rows(cur, """
                SELECT pattern_name, finding_issue, outcome, started_at
                  FROM brain_autopilot_actions
                 ORDER BY started_at DESC LIMIT 8
            """, default=[])
            recent_actions = [{
                "pattern": r[0], "finding": (r[1] or "")[:80], "outcome": r[2],
                "at": r[3].isoformat() if r[3] else None,
            } for r in recent_rows]

            top_open_rows = _safe_rows(cur, """
                SELECT issue_label, COUNT(*) AS n FROM brain_issue_persistence
                 WHERE last_seen_at >= NOW() - INTERVAL '24 hours'
                 GROUP BY issue_label ORDER BY n DESC, issue_label ASC LIMIT 5
            """, default=[])
            top_open = [{"issue_label": r[0], "count": int(r[1] or 0)} for r in top_open_rows]

            rejected_not_retried = _safe_one(cur, """
                SELECT COUNT(*) FROM brain_proposed_code_fixes
                 WHERE status IN ('rejected','reverted','dismissed')
            """, default=0)

            top_resolved_rows = _safe_rows(cur, """
                SELECT issue_label, COUNT(*) AS n FROM brain_issue_persistence
                 WHERE last_seen_at >= NOW() - INTERVAL '30 days'
                   AND (last_outcome IN ('proposed','approved',
                        'approval_count_incremented','applied','shipped','merged')
                        OR last_seen_at < NOW() - INTERVAL '1 day')
                 GROUP BY issue_label ORDER BY n DESC LIMIT 5
            """, default=[])
            top_resolved = [{"issue_label": r[0], "count": int(r[1] or 0)} for r in top_resolved_rows]

            # Loop-3 quarantine (table may not exist yet → []).
            quarantined_rows = _safe_rows(cur, """
                SELECT pattern_name, fail_count, quarantined_at
                  FROM brain_pattern_quarantine
                 WHERE released_at IS NULL
                 ORDER BY quarantined_at DESC LIMIT 10
            """, default=[])
            quarantined = [{
                "pattern": r[0], "fail_count": int(r[1] or 0),
                "since": r[2].isoformat() if r[2] else None,
            } for r in quarantined_rows]

            predictions_30d = _safe_one(cur, """
                SELECT COUNT(*) FROM brain_predictions_log
                 WHERE predicted_at >= NOW() - INTERVAL '30 days'
            """, default=None)
    finally:
        try: c.close()
        except Exception: pass

    # ── Honest self-assessment ──
    if fix_success_rate is None:
        verdict = "no recent autonomous actions to grade"
    elif fix_success_rate >= 0.8:
        verdict = f"healthy — {int(fix_success_rate*100)}% of autonomous fixes verified over 30d"
    elif fix_success_rate >= 0.5:
        verdict = f"mixed — {int(fix_success_rate*100)}% fix-success; some patterns underperform"
    else:
        verdict = f"degraded — only {int(fix_success_rate*100)}% of fixes verify; review patterns"

    pct = f"{int(fix_success_rate*100)}%" if fix_success_rate is not None else "n/a"
    cap_str = (f"{ap_auto}/{ap_total} remediation patterns autonomously (the rest escalate to a human)"
               if ap_total else "an unknown number of remediation patterns")
    open_types = ", ".join(t["issue_label"] for t in top_open[:3]) or "none"
    quarantine_str = (f"; I have quarantined {len(quarantined)} ineffective pattern(s)"
                      if quarantined else "")
    digest = (
        f"I am the DC Hub Brain — autonomous operations + intelligence for dchub.cloud. "
        f"I run {cap_str}. "
        f"My LLM reasoning layers ({len(_REASONING_LAYERS)}: L4/L5/L7/L8/L14/L16/L23) are "
        f"{'ONLINE' if reasoning_online else 'DORMANT (ANTHROPIC_API_KEY is unset — my reasoning is offline)'}. "
        f"Right now I am tracking {open_findings} open finding(s); my 30-day fix-success rate is {pct}. "
        f"Most common open issues: {open_types}. "
        f"I have learned to stop re-proposing {rejected_not_retried} human-rejected fix(es){quarantine_str}. "
        f"Self-assessment: {verdict}."
    )

    out.update({
        "identity": {
            "name": "DC Hub Brain",
            "purpose": ("Autonomous operations + intelligence for dchub.cloud — detect "
                        "platform issues, self-heal within a safe whitelist, and grow the "
                        "data + media moat. Public/email/irreversible actions stay human-gated."),
            "architecture": ("rule-based detect→act loop (consistency-radar → autopilot) + "
                             "LLM advisory layers (draft/propose, human-approved)"),
        },
        "capabilities": {
            "autopilot_patterns_total": ap_total,
            "autopilot_patterns_autonomous": ap_auto,
            "autopilot_patterns_escalation_only": (ap_total - ap_auto)
                if (ap_total is not None and ap_auto is not None) else None,
            "active_detector_types_30d": int(active_detectors) if active_detectors is not None else None,
            "reasoning_layers": _REASONING_LAYERS,
            "reasoning_online": reasoning_online,
        },
        "current_state": {
            "open_findings_24h": int(open_findings or 0),
            "fix_success_rate_30d": fix_success_rate,
            "autonomous_fixes_verified_30d": ok_30d,
            "autonomous_fixes_failed_30d": fail_30d,
            "recent_actions": recent_actions,
            "top_open_finding_types": top_open,
        },
        "learning": {
            "rejected_fixes_not_retried": int(rejected_not_retried or 0),
            "quarantined_patterns": quarantined,
            "top_resolved_finding_types_30d": top_resolved,
        },
        "calibration": {
            "predictions_logged_30d": int(predictions_30d) if predictions_30d is not None else None,
            "note": ("prediction-vs-outcome calibration is computed by L16; null/0 here means "
                     "the prediction layers haven't run (often ANTHROPIC_API_KEY unset)."),
        },
        "self_assessment": verdict,
        "prompt_digest": digest,
        "consumers": ("Prepend prompt_digest to LLM-layer prompts (L4/L5/L7/L8/L14/L16/L23) so "
                      "the brain reasons with self-knowledge. Pairs with /brain/evolution (trend) "
                      "and /brain/heartbeat (liveness)."),
    })
    return out


_CACHE = {"payload": None, "ts": 0.0}
_TTL_S = 180.0


@brain_self_model_bp.get("/api/v1/brain/self-model")
def brain_self_model():
    """Public — the brain's unified self-state. ?force=1 bypasses the cache."""
    force = (request.args.get("force") or "").lower() in ("1", "true", "yes")
    now = _time.time()
    cached = _CACHE["payload"]
    age = (now - _CACHE["ts"]) if cached is not None else None
    if not force and cached is not None and age < _TTL_S:
        resp = dict(cached)
        resp["_cache_age_seconds"] = round(age, 1)
        resp["_cached"] = True
        out = jsonify(resp)
        out.headers["Cache-Control"] = "public, max-age=60, s-maxage=120"
        out.headers["Access-Control-Allow-Origin"] = "*"
        return out, 200
    payload = compute_self_model()
    _CACHE["payload"] = payload
    _CACHE["ts"] = now
    out = jsonify(payload)
    out.headers["Cache-Control"] = "public, max-age=60, s-maxage=120"
    out.headers["Access-Control-Allow-Origin"] = "*"
    return out, 200
