"""Shell #35 (2026-07-26) — grid/fiber HEAVY-USER radar (measurement only).

The brain's L6 metering/upsell proposals need one missing input: WHICH
free/identified keys are heavy on the two proven-demand tool families.
This radar measures it weekly from mcp_call_log and files findings so
the owner/brain can decide metering (actual billing changes stay
human-gated per autonomy-core). No response shaping, no gate changes —
the live teaser/upgrade gates already cover free tier.

Weekly gate: skips if the summary finding was updated <6 days ago.
Trigger: STEP 4 of the daily competitors/scan chain (house pattern:
one cron, multiple steps). Endpoint: GET /api/v1/monetize/grid-fiber-radar
(X-Admin-Key).
"""

from __future__ import annotations

import os
import hashlib
import logging

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

grid_fiber_radar_bp = Blueprint("grid_fiber_radar", __name__)

_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY")
              or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()

TOOLS = ("get_grid_intelligence", "get_fiber_intel", "get_grid_data",
         "get_metro_fiber")
THRESHOLD = int(os.environ.get("GRID_FIBER_HEAVY_THRESHOLD", "50"))
_DAYS = 30


def _conn():
    import psycopg2
    db = os.environ.get("DATABASE_URL")
    if not db:
        return None
    try:
        return psycopg2.connect(db, sslmode="require", connect_timeout=5)
    except Exception:
        return None


def _ran_recently(cur) -> bool:
    try:
        cur.execute("SELECT last_seen > NOW() - INTERVAL '6 days' "
                    "FROM brain_findings "
                    "WHERE issue = 'monetize:grid_fiber_radar_summary' "
                    "ORDER BY last_seen DESC LIMIT 1")
        row = cur.fetchone()
        return bool(row and row[0])
    except Exception:
        return False


def run_usage_radar(force: bool = False) -> dict:
    out = {"status": "ok", "heavy_users": [], "findings_filed": 0}
    c = _conn()
    if c is None:
        return {"status": "no_database"}
    try:
        with c.cursor() as cur:
            if not force and _ran_recently(cur):
                return {"status": "skipped_recent"}
            c.rollback()  # clear any aborted probe state before real work
            cur.execute("""
                SELECT api_key, COALESCE(tier,'free') AS tier,
                       COUNT(*) AS calls, COUNT(DISTINCT tool) AS tools_used
                  FROM mcp_call_log
                 WHERE timestamp >= NOW() - make_interval(days => %s)
                   AND tool = ANY(%s)
                   AND api_key IS NOT NULL AND api_key <> ''
                   AND COALESCE(tier,'free') IN ('free','identified','trial')
                 GROUP BY api_key, COALESCE(tier,'free')
                HAVING COUNT(*) >= %s
                 ORDER BY calls DESC LIMIT 50
            """, (_DAYS, list(TOOLS), THRESHOLD))
            rows = cur.fetchall()
        heavy = []
        for api_key, tier, calls, tools_used in rows:
            kid = hashlib.sha256(api_key.encode()).hexdigest()[:10]
            heavy.append({"key_id": kid, "tier": tier, "calls_30d": int(calls),
                          "tools_used": int(tools_used)})
        out["heavy_users"] = heavy
        try:
            from routes.brain_findings_writer import upsert_brain_finding
            with c.cursor() as cur:
                for h in heavy[:10]:
                    upsert_brain_finding(
                        cur,
                        issue=f"monetize:grid_fiber_heavy:{h['key_id']}",
                        url="dchub://monetize/grid-fiber-radar",
                        count=h["calls_30d"],
                        detail=(f"[{h['tier']}] {h['calls_30d']} grid/fiber "
                                f"calls/30d across {h['tools_used']} tools — "
                                f"metering candidate (owner-gated)")[:2000],
                        detector="grid_fiber_usage_radar", status="open")
                    out["findings_filed"] += 1
                upsert_brain_finding(
                    cur, issue="monetize:grid_fiber_radar_summary",
                    url="dchub://monetize/grid-fiber-radar",
                    count=len(heavy),
                    detail=(f"{len(heavy)} keys >= {THRESHOLD} grid/fiber "
                            f"calls/30d (free/identified/trial). Top candidates "
                            f"filed as monetize:grid_fiber_heavy:*")[:2000],
                    detector="grid_fiber_usage_radar", status="resolved")
                out["findings_filed"] += 1
            c.commit()
        except Exception as e:
            try:
                c.rollback()
            except Exception:
                pass
            out["status"] = "partial"
            out["error"] = str(e)[:160]
    finally:
        try:
            c.close()
        except Exception:
            pass
    logger.info("grid_fiber_usage_radar: %s heavy, filed %s",
                len(out.get("heavy_users", [])), out.get("findings_filed"))
    return out


@grid_fiber_radar_bp.route("/api/v1/monetize/grid-fiber-radar", methods=["GET", "POST"])
def radar_endpoint():
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if _ADMIN_KEY and provided != _ADMIN_KEY:
        return jsonify(error="unauthorized"), 401
    return jsonify(run_usage_radar(force=request.args.get("force") == "1")), 200
