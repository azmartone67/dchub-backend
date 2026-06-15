"""DC Hub — retention endpoint (r86-reach, 2026-06-14).

STANDALONE drop-in so it survives the parallel session's churn of mcp_funnel.py.
Built + verified live this session (returned 7.4% reuse / 0.62 calls/key / 29 new-1 returning),
then reverted by a concurrent perf-refactor session. To restore durably:

  1. Save this file as routes/mcp_retention.py
  2. In main.py, near the other blueprint registrations, add:
        try:
            from routes.mcp_retention import mcp_retention_bp
            app.register_blueprint(mcp_retention_bp)
        except Exception as _e:
            logging.getLogger(__name__).warning('mcp_retention wiring failed: %s', _e)
  3. railway up  (coordinate so it doesn't race the other session)
  4. Verify: curl .../api/v1/mcp/retention?weeks=8

This file touches NO contested files, so the other session won't revert it.
"""
from __future__ import annotations
import os
from flask import Blueprint, jsonify, request
import psycopg2, psycopg2.extras

mcp_retention_bp = Blueprint("mcp_retention_r86", __name__)
_INTERNAL = r"(loop|dchub-|selfheal|probe|health|scanner|regression|mcp-test|sweep|clawith|anthropicapi)"


def _conn():
    db = os.environ.get("DATABASE_URL")
    if not db:
        return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


@mcp_retention_bp.route("/api/v1/mcp/retention", methods=["GET"])
def mcp_retention():
    try:
        weeks = max(1, min(26, int(request.args.get("weeks") or 11)))
    except ValueError:
        weeks = 11
    c = _conn()
    if c is None:
        return jsonify(error="no_db"), 503
    out = {"weeks": weeks, "ip_cohort": [], "key_reuse": [], "summary": {},
           "note": ("Retention is the lever, not reach: inflow ~30-50 new ext IPs/wk but ~1 returns. "
                    "Watch pct_reused + returning_ips climb = the r86 first-touch fix working. "
                    "Mint VOLUME is scan/anon-inflated — read the RATE, not the count.")}
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                WITH ext AS (
                  SELECT ip_address, date_trunc('week', created_at) AS wk,
                         MIN(date_trunc('week', created_at)) OVER (PARTITION BY ip_address) AS first_wk
                  FROM mcp_tool_calls
                  WHERE created_at >= now() - (%s || ' weeks')::interval
                    AND ip_address IS NOT NULL AND ip_address <> ''
                    AND COALESCE(client_name,'') !~* %s AND COALESCE(platform,'') !~* %s )
                SELECT wk::date AS week, COUNT(DISTINCT ip_address) AS distinct_ips,
                       COUNT(DISTINCT ip_address) FILTER (WHERE wk = first_wk) AS new_ips,
                       COUNT(DISTINCT ip_address) FILTER (WHERE wk > first_wk) AS returning_ips
                FROM ext GROUP BY wk ORDER BY wk
            """, (weeks, _INTERNAL, _INTERNAL))
            out["ip_cohort"] = [dict(r) for r in cur.fetchall()]
            cur.execute("""
                SELECT date_trunc('week', minted_at)::date AS week, COUNT(*) AS minted,
                       COUNT(*) FILTER (WHERE call_count > 1) AS reused_2plus,
                       COUNT(*) FILTER (WHERE last_used_at IS NOT NULL
                                AND last_used_at > minted_at + interval '1 hour') AS returned_later,
                       COUNT(DISTINCT request_ip_hash) AS distinct_ips
                FROM auto_trial_keys WHERE minted_at >= now() - (%s || ' weeks')::interval
                GROUP BY week ORDER BY week
            """, (weeks,))
            out["key_reuse"] = [dict(r) for r in cur.fetchall()]
            cur.execute("""
                SELECT COUNT(*) AS minted_30d,
                       ROUND(100.0*COUNT(*) FILTER (WHERE call_count > 1)/NULLIF(COUNT(*),0),1) AS pct_reused_30d,
                       ROUND(AVG(call_count),2) AS avg_calls_per_key_30d
                FROM auto_trial_keys WHERE minted_at >= now() - interval '30 days'
            """)
            row = cur.fetchone()
            out["summary"] = dict(row) if row else {}
            # r86b (2026-06-14): NEVER let the in-progress current week read as a
            # "decline". date_trunc('week', now()) = Monday of the current ISO week;
            # any cohort/reuse row with week >= that is a PARTIAL week (often just a
            # handful of UTC-edge calls → 0 new / ~0 returning) and was making the
            # headline KPI + the last table row look like a cliff. Split it out:
            # the trend arrays + latest_* headline use only COMPLETE weeks; the
            # partial week is surfaced separately under current_partial_* so nothing
            # is hidden, just not mistaken for a finished data point.
            cur.execute("SELECT date_trunc('week', now())::date AS cur_wk")
            cur_wk = cur.fetchone()["cur_wk"]
            partial_ip = [r for r in out["ip_cohort"] if r["week"] >= cur_wk]
            out["ip_cohort"] = [r for r in out["ip_cohort"] if r["week"] < cur_wk]
            out["key_reuse"] = [r for r in out["key_reuse"] if r["week"] < cur_wk]
            if out["ip_cohort"]:
                last = out["ip_cohort"][-1]
                out["summary"].update(latest_week=str(last["week"]),
                                      latest_new_ips=last["new_ips"],
                                      latest_returning_ips=last["returning_ips"],
                                      latest_complete_week=str(last["week"]))
            if partial_ip:
                pw = partial_ip[-1]
                out["summary"].update(current_partial_week=str(pw["week"]),
                                      current_partial_new_ips=pw["new_ips"],
                                      current_partial_returning_ips=pw["returning_ips"])
            else:
                out["summary"]["current_partial_week"] = str(cur_wk)
    except Exception as e:
        return jsonify(error="query_failed", detail=str(e)[:200]), 500
    finally:
        try:
            c.close()
        except Exception:
            pass
    return jsonify(out), 200
