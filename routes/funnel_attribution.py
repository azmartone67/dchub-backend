"""Phase JJJJJ (2026-05-16) — anon→signup attribution chain.

Today we measure each funnel stage in isolation:
  - mcp_call_log:           tool hits per (ip+ua)
  - anon_grace_log:         grace grants per (ip+ua)
  - auto_trial_keys:        trial keys per (ip+ua)
  - api_keys:               permanent signups by email
  - mcp_pair_codes:         redeemed → paid conversions

We don't connect them. JJJJJ joins via request_ip_hash on the
auto_trial_keys table, building per-caller funnels so we can ask:

  - How many anon callers became permanent signups this week?
  - Which tool drove the most successful conversions?
  - Which IP+UA combinations are paying customers today?

  GET /api/v1/funnel/attribution      full-funnel summary
  GET /api/v1/funnel/by-tool          which tool drove most conversions
  GET /api/v1/funnel/chain/<key>      trace one trial key end-to-end
"""

from __future__ import annotations

import os
import datetime
from flask import Blueprint, jsonify, request


funnel_attribution_bp = Blueprint("funnel_attribution", __name__)


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


@funnel_attribution_bp.route("/api/v1/funnel/attribution", methods=["GET"])
def attribution_summary():
    """End-to-end funnel rollup over the last 30 days. Joins:
      anon_grace_log → auto_trial_keys → signed_up → upgraded."""
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    out: dict = {
        "window":           "30 days",
        "stages": {
            "anon_unique_callers":     0,
            "trials_minted":           0,
            "trials_used_2plus_calls": 0,
            "trials_signed_up":        0,
            "trials_upgraded":         0,
        },
        "rates": {},
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
    }
    try:
        with c.cursor() as cur:
            # Trials minted
            try:
                cur.execute("""
                    SELECT to_regclass('public.auto_trial_keys')
                """)
                if (cur.fetchone() or [None])[0]:
                    cur.execute("""
                        SELECT
                          COUNT(*) AS minted,
                          COUNT(DISTINCT request_ip_hash) AS unique_callers,
                          COUNT(*) FILTER (WHERE call_count >= 2) AS used_2plus,
                          COUNT(*) FILTER (WHERE signed_up_email IS NOT NULL) AS signed_up,
                          COUNT(*) FILTER (WHERE upgraded_tier IS NOT NULL) AS upgraded
                          FROM auto_trial_keys
                         WHERE minted_at >= NOW() - INTERVAL '30 days'
                    """)
                    r = cur.fetchone() or (0, 0, 0, 0, 0)
                    s = out["stages"]
                    s["trials_minted"]           = int(r[0] or 0)
                    s["anon_unique_callers"]     = int(r[1] or 0)
                    s["trials_used_2plus_calls"] = int(r[2] or 0)
                    s["trials_signed_up"]        = int(r[3] or 0)
                    s["trials_upgraded"]         = int(r[4] or 0)
            except Exception:
                pass
    finally:
        try: c.close()
        except Exception: pass

    s = out["stages"]
    minted = max(1, s.get("trials_minted") or 1)
    callers = max(1, s.get("anon_unique_callers") or 1)
    out["rates"] = {
        "calls_to_2plus_use_pct":  round(100.0 * s["trials_used_2plus_calls"] / minted, 1),
        "trial_to_signup_pct":     round(100.0 * s["trials_signed_up"] / minted, 1),
        "trial_to_upgrade_pct":    round(100.0 * s["trials_upgraded"] / minted, 1),
        "signup_to_upgrade_pct":   round(100.0 * s["trials_upgraded"] / max(1, s["trials_signed_up"]), 1),
    }
    out["interpretation"] = (
        f"Of {minted:,} trial keys minted in last 30d, "
        f"{s['trials_used_2plus_calls']} agents retried with their key "
        f"({out['rates']['calls_to_2plus_use_pct']}%), "
        f"{s['trials_signed_up']} converted to permanent IDENTIFIED "
        f"({out['rates']['trial_to_signup_pct']}%), and "
        f"{s['trials_upgraded']} upgraded to paid "
        f"({out['rates']['trial_to_upgrade_pct']}% of trials)."
    )
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "public, max-age=300"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


@funnel_attribution_bp.route("/api/v1/funnel/by-tool", methods=["GET"])
def by_tool():
    """Which tool drove the most successful conversions? Joins
    auto_trial_keys.minted_for_tool ← the tool that triggered the
    paywall ← upstream tool call."""
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            try:
                # Phase FF+23-followup (2026-05-20): RealDictCursor returns
                # dict, not tuple — `[0]` was raising KeyError on every call,
                # producing `{"error":"query_failed:KeyError"}` and a 503 at
                # the worker layer. Use the column name instead.
                cur.execute("SELECT to_regclass('public.auto_trial_keys') AS t")
                _row = cur.fetchone()
                if not _row or not _row.get("t"):
                    return jsonify(tools=[], note="no auto_trial_keys yet"), 200
                cur.execute("""
                    SELECT minted_for_tool,
                           COUNT(*) AS minted,
                           COUNT(*) FILTER (WHERE signed_up_email IS NOT NULL) AS signed_up,
                           COUNT(*) FILTER (WHERE upgraded_tier IS NOT NULL) AS upgraded,
                           COUNT(*) FILTER (WHERE call_count >= 2) AS retried
                      FROM auto_trial_keys
                     WHERE minted_at >= NOW() - INTERVAL '30 days'
                       AND minted_for_tool IS NOT NULL
                     GROUP BY minted_for_tool
                     ORDER BY minted DESC LIMIT 20
                """)
                rows = cur.fetchall()
            except Exception as e:
                return jsonify(error=f"query_failed:{type(e).__name__}"), 500
    finally:
        try: c.close()
        except Exception: pass
    out = [{
        "tool":              r["minted_for_tool"],
        "minted":            int(r["minted"] or 0),
        "retried":           int(r["retried"] or 0),
        "signed_up":         int(r["signed_up"] or 0),
        "upgraded":          int(r["upgraded"] or 0),
        "retry_rate_pct":    round(100.0 * (r["retried"] or 0) / max(1, r["minted"] or 1), 1),
        "signup_rate_pct":   round(100.0 * (r["signed_up"] or 0) / max(1, r["minted"] or 1), 1),
        "upgrade_rate_pct":  round(100.0 * (r["upgraded"] or 0) / max(1, r["minted"] or 1), 1),
    } for r in rows]
    resp = jsonify(tools=out, count=len(out),
                   generated_at=datetime.datetime.utcnow().isoformat() + "Z")
    resp.headers["Cache-Control"] = "public, max-age=300"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


@funnel_attribution_bp.route("/api/v1/funnel/chain/<api_key>", methods=["GET"])
def trace_chain(api_key):
    """Trace one trial key's full journey: minted → calls → signup
    → upgrade. Useful for debugging a specific conversion path."""
    if not api_key.startswith("dch_trial_"):
        return jsonify(error="not_a_trial_key"), 400
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            try:
                cur.execute("""
                    SELECT api_key, minted_at, expires_at, minted_for_tool,
                           request_ip_hash, request_ua, last_used_at,
                           call_count, signed_up_email, upgraded_tier
                      FROM auto_trial_keys WHERE api_key = %s
                """, (api_key,))
                r = cur.fetchone()
                if not r: return jsonify(error="trial_not_found"), 404
                # Find grace grants from the same ip_hash (likely the
                # same caller in their pre-mint phase)
                grace_count = 0
                try:
                    cur.execute("""
                        SELECT COUNT(*) FROM anon_grace_log
                         WHERE caller_hash = %s
                           AND granted_at <= %s
                    """, (r["request_ip_hash"], r["minted_at"]))
                    grace_count = int((cur.fetchone() or [0])[0] or 0)
                except Exception: pass
                return jsonify({
                    "api_key":         r["api_key"],
                    "minted_at":       r["minted_at"].isoformat() if r["minted_at"] else None,
                    "expires_at":      r["expires_at"].isoformat() if r["expires_at"] else None,
                    "minted_for_tool": r["minted_for_tool"],
                    "ip_hash":         r["request_ip_hash"],
                    "ua":              (r["request_ua"] or "")[:80],
                    "last_used_at":    r["last_used_at"].isoformat() if r["last_used_at"] else None,
                    "call_count":      int(r["call_count"] or 0),
                    "grace_calls_before_mint": grace_count,
                    "signed_up_email": r["signed_up_email"],
                    "upgraded_tier":   r["upgraded_tier"],
                    "verdict": (
                        "upgraded" if r["upgraded_tier"]
                        else "signed_up" if r["signed_up_email"]
                        else "active_trial" if r["call_count"] and r["call_count"] >= 2
                        else "minted_unused"
                    ),
                }), 200
            except Exception as e:
                return jsonify(error=f"query_failed:{type(e).__name__}"), 500
    finally:
        try: c.close()
        except Exception: pass
