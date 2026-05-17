"""Phase FFFFF (2026-05-16) — autopilot resolution outcome verification.

The brain knows which actions it FIRED but not which ones SUCCEEDED.
When dchub_media_press_silent fires marketing/auto-generate, did a
new press row actually land? When winback_pitches_unsent fires the
delivery endpoint, did Resend actually send? Today: invisible.

This module:
  1. Defines per-pattern outcome verifiers (one function per pattern)
  2. Runs verifications 30+ min after each autopilot action fires
  3. Persists outcomes to autopilot_outcomes table
  4. Brain detector autopilot_action_unverified fires for stale ones

  POST /api/v1/brain/autopilot/verify-pending   admin cron entry
  GET  /api/v1/brain/autopilot/outcomes         public outcome history

Cron piggy-backs on heartbeat-auto (every 15min): each tick verifies
actions fired 30+ min ago.
"""

from __future__ import annotations

import os
import datetime
from flask import Blueprint, jsonify, request


autopilot_outcomes_bp = Blueprint("autopilot_outcomes", __name__)


_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY")
              or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()


def _conn():
    import psycopg2
    db = os.environ.get("DATABASE_URL")
    if not db: return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS autopilot_outcomes (
    id                BIGSERIAL PRIMARY KEY,
    autopilot_action_id BIGINT,        -- FK reference to brain_autopilot_actions.id
    pattern_name      TEXT NOT NULL,
    fired_at          TIMESTAMPTZ NOT NULL,
    verified_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    succeeded         BOOLEAN NOT NULL,
    evidence          TEXT,              -- what we checked and found
    failure_reason    TEXT
);
CREATE INDEX IF NOT EXISTS ix_autopilot_outcomes_pattern
    ON autopilot_outcomes(pattern_name, verified_at DESC);
CREATE INDEX IF NOT EXISTS ix_autopilot_outcomes_action
    ON autopilot_outcomes(autopilot_action_id);
"""


def _ensure_schema(c):
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
    except Exception:
        try: c.rollback()
        except Exception: pass


# Per-pattern verifier: returns (succeeded, evidence_str).
# Each takes the autopilot_actions row dict and DB cursor.

def _verify_press_silent(action, cur) -> tuple[bool, str]:
    """marketing/auto-generate — did a new press row land in 30 min?"""
    try:
        cur.execute("""
            SELECT COUNT(*) FROM auto_press_releases
             WHERE COALESCE(generated_for, created_at)
                    >= %s
               AND COALESCE(generated_for, created_at)
                    <= %s + INTERVAL '60 minutes'
        """, (action["started_at"], action["started_at"]))
        n = int((cur.fetchone() or [0])[0] or 0)
        return (n > 0, f"{n} new press release(s) in the 60min after action fired")
    except Exception as e:
        return (False, f"verify_query_failed:{type(e).__name__}")


def _verify_winback_delivery(action, cur) -> tuple[bool, str]:
    """winback/deliver — did a row land in winback_outreach_sent?"""
    try:
        cur.execute("""
            SELECT COUNT(*) FROM winback_outreach_sent
             WHERE sent_at >= %s - INTERVAL '5 minutes'
               AND sent_at <= %s + INTERVAL '15 minutes'
        """, (action["started_at"], action["started_at"]))
        n = int((cur.fetchone() or [0])[0] or 0)
        return (n > 0, f"{n} winback delivery row(s) recorded")
    except Exception as e:
        return (False, f"verify_query_failed:{type(e).__name__}")


def _verify_market_deep_dive(action, cur) -> tuple[bool, str]:
    """markets/deep-dive/cron — did any rows update?"""
    try:
        cur.execute("""
            SELECT COUNT(*) FROM market_deep_dives
             WHERE generated_at >= %s - INTERVAL '5 minutes'
               AND generated_at <= %s + INTERVAL '30 minutes'
        """, (action["started_at"], action["started_at"]))
        n = int((cur.fetchone() or [0])[0] or 0)
        return (n > 0, f"{n} market deep-dive(s) generated")
    except Exception as e:
        return (False, f"verify_query_failed:{type(e).__name__}")


def _verify_competitor_response(action, cur) -> tuple[bool, str]:
    """competitor_announcement → marketing/auto-generate — same as press_silent"""
    return _verify_press_silent(action, cur)


# Pattern → verifier mapping. Patterns without a verifier are skipped
# (treated as 'cannot verify' — different from failed).
_VERIFIERS = {
    "dchub_media_press_silent":   _verify_press_silent,
    "winback_pitches_unsent":     _verify_winback_delivery,
    "market_deep_dive_stale":     _verify_market_deep_dive,
    "competitor_announcement":    _verify_competitor_response,
}


def verify_pending(window_minutes: int = 30, max_actions: int = 50) -> dict:
    """Find autopilot actions fired in the last `window_minutes` to N
    hours ago that haven't been verified yet, run their verifier,
    persist outcomes."""
    out: dict = {"verified": 0, "succeeded": 0, "failed": 0,
                  "skipped": 0, "details": [],
                  "ran_at": datetime.datetime.utcnow().isoformat() + "Z"}
    c = _conn()
    if c is None:
        out["error"] = "no_database"; return out
    try:
        _ensure_schema(c)
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Find unverified actions in the verification window
            try:
                cur.execute("""
                    SELECT a.id, a.pattern_name, a.started_at, a.outcome
                      FROM brain_autopilot_actions a
                     WHERE a.started_at <= NOW() - INTERVAL '%s minutes'
                       AND a.started_at >= NOW() - INTERVAL '6 hours'
                       AND a.outcome = 'executed_ok'
                       AND NOT EXISTS (
                         SELECT 1 FROM autopilot_outcomes o
                          WHERE o.autopilot_action_id = a.id
                       )
                     ORDER BY a.started_at DESC LIMIT %s
                """, (window_minutes, max_actions))
                actions = cur.fetchall()
            except Exception as e:
                out["error"] = f"action_query_failed:{type(e).__name__}"
                return out

            for a in actions:
                pattern = a["pattern_name"] or ""
                verifier = _VERIFIERS.get(pattern)
                if not verifier:
                    out["skipped"] += 1
                    continue
                succeeded, evidence = verifier(a, cur)
                out["verified"] += 1
                if succeeded: out["succeeded"] += 1
                else: out["failed"] += 1
                try:
                    cur.execute("""
                        INSERT INTO autopilot_outcomes
                          (autopilot_action_id, pattern_name, fired_at,
                           succeeded, evidence, failure_reason)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT DO NOTHING
                    """, (a["id"], pattern, a["started_at"], succeeded,
                          evidence, (None if succeeded else evidence)))
                except Exception: pass
                out["details"].append({
                    "action_id": int(a["id"]),
                    "pattern":   pattern,
                    "fired_at":  a["started_at"].isoformat() if a["started_at"] else None,
                    "succeeded": succeeded,
                    "evidence":  evidence,
                })
    finally:
        try: c.close()
        except Exception: pass
    return out


@autopilot_outcomes_bp.route("/api/v1/brain/autopilot/verify-pending", methods=["POST"])
def verify_endpoint():
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if _ADMIN_KEY and provided != _ADMIN_KEY:
        return jsonify(error="unauthorized"), 401
    return jsonify(verify_pending()), 200


@autopilot_outcomes_bp.route("/api/v1/brain/autopilot/outcomes", methods=["GET"])
def outcomes_endpoint():
    """Public outcome history — what autopilot actions actually worked."""
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        _ensure_schema(c)
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT pattern_name,
                       COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE succeeded) AS succeeded,
                       MAX(verified_at) AS last_verified
                  FROM autopilot_outcomes
                 WHERE verified_at >= NOW() - INTERVAL '30 days'
                 GROUP BY pattern_name
                 ORDER BY total DESC
            """)
            by_pattern = cur.fetchall()
            cur.execute("""
                SELECT pattern_name, fired_at, verified_at,
                       succeeded, evidence
                  FROM autopilot_outcomes
                 ORDER BY verified_at DESC LIMIT 50
            """)
            recent = cur.fetchall()
    finally:
        try: c.close()
        except Exception: pass
    return jsonify({
        "by_pattern": [{
            "pattern":          r["pattern_name"],
            "total_verified":   int(r["total"] or 0),
            "succeeded":        int(r["succeeded"] or 0),
            "success_rate_pct": round(100.0 * (r["succeeded"] or 0) / max(1, r["total"] or 1), 1),
            "last_verified":    r["last_verified"].isoformat() if r["last_verified"] else None,
        } for r in by_pattern],
        "recent": [{
            "pattern":     r["pattern_name"],
            "fired_at":    r["fired_at"].isoformat() if r["fired_at"] else None,
            "verified_at": r["verified_at"].isoformat() if r["verified_at"] else None,
            "succeeded":   bool(r["succeeded"]),
            "evidence":    r["evidence"],
        } for r in recent],
    }), 200
