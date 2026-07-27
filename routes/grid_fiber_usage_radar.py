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
            # ★★ 2026-07-27 CORRECTION. The first version grouped by
            # (api_key, COALESCE(tier,'free')) and flagged the top caller as
            # a free-tier freeloader with 1,215 calls. It was OUR OWN key —
            # `dchub_live_08f…`, api_keys.name='DCHUB', plan='pro', with
            # 165,201 of its 180,282 monthly calls already logged tier=paid.
            # Only the rows whose tier wasn't resolved at log time said
            # 'free', and grouping BY tier split one key into a phantom
            # free caller. Metering it would have throttled internal traffic
            # and monitoring for zero revenue.
            # Now: judge a key by its WHOLE 30 days, take the tier from
            # api_keys (authoritative) not the per-row log value, and
            # exclude first-party/self traffic outright.
            cur.execute("""
                WITH per_key AS (
                    SELECT l.api_key,
                           COUNT(*) AS calls,
                           COUNT(DISTINCT l.tool) AS tools_used,
                           COUNT(*) FILTER (WHERE l.tier = 'paid') AS paid_rows,
                           BOOL_OR(COALESCE(l.platform,'') = 'dchub-internal'
                                   OR COALESCE(l.user_agent,'') ILIKE '%%dchub%%'
                                   OR COALESCE(l.user_agent,'') ILIKE '%%curl%%'
                                  ) AS self_traffic
                      FROM mcp_call_log l
                     WHERE l.timestamp >= NOW() - make_interval(days => %s)
                       AND l.tool = ANY(%s)
                       AND l.api_key IS NOT NULL AND l.api_key <> ''
                       -- attribution bug: some rows store an IP here
                       AND l.api_key NOT SIMILAR TO '[0-9]{1,3}[.][0-9]%%'
                     GROUP BY l.api_key
                )
                SELECT k.api_key, k.calls, k.tools_used
                  FROM per_key k
                 WHERE k.calls >= %s
                   AND k.paid_rows = 0          -- ANY paid row ⇒ not a free rider
                   AND NOT k.self_traffic
                   -- first-party prefix: dchub_live_* is ours; the
                   -- self-serve free keys are dch_live_*
                   AND k.api_key NOT LIKE 'dchub[_]%%'
                 ORDER BY k.calls DESC LIMIT 50
            """, (_DAYS, list(TOOLS), THRESHOLD))
            candidates = cur.fetchall()

        # Identity resolution in Python: this Postgres has NO pgcrypto, so
        # digest() is unavailable and the hash join must happen here.
        # api_keys is the AUTHORITATIVE tier source — the per-row tier in
        # mcp_call_log is unreliable (that's what caused the false flag).
        rows = []
        with c.cursor() as cur:
            for api_key, calls, tools_used in candidates:
                kh = hashlib.sha256(api_key.encode()).hexdigest()
                plan, key_name = "unknown", ""
                try:
                    cur.execute("SELECT COALESCE(plan,''), COALESCE(name,'') "
                                "FROM api_keys WHERE key_hash = %s LIMIT 1",
                                (kh,))
                    r = cur.fetchone()
                    if r:
                        plan, key_name = (r[0] or "unknown"), (r[1] or "")
                except Exception:
                    c.rollback()
                if str(plan).lower() in ("pro", "paid", "enterprise",
                                         "founding", "developer", "partner"):
                    continue
                if "dchub" in key_name.lower():
                    continue
                rows.append((api_key, calls, tools_used, plan, key_name))
        heavy = []
        for api_key, calls, tools_used, plan, key_name in rows:
            kid = hashlib.sha256(api_key.encode()).hexdigest()[:10]
            heavy.append({"key_id": kid, "tier": plan, "calls_30d": int(calls),
                          "tools_used": int(tools_used),
                          "key_name": key_name or None})
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
                        detail=(f"[plan={h['tier']}] {h['calls_30d']} "
                                f"grid/fiber calls/30d across "
                                f"{h['tools_used']} tools, zero paid rows, "
                                f"not self-traffic — metering candidate "
                                f"(owner-gated). Verify the key's identity "
                                f"in api_keys before acting: the first cut of "
                                f"this radar flagged our own DCHUB pro key."
                                )[:2000],
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
