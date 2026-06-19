"""routes/mcp_anon_usage.py — anonymous per-IP daily call count (2026-06-18).

Read side of the SOFT anonymous-daily-cap nudge. The MCP gateway (server.mjs)
calls this with the TRUE client IP (c.client_ip) to learn how many tool calls
that anonymous IP has already made today; if it's at/over DCHUB_ANON_DAILY_CAP
the gateway returns the same trimForTrial preview body plus an `_upgrade`
escalation (remaining_today:0, next_tool: claim_free_key). This endpoint only
COUNTS — all policy lives in the gateway, behind its own OFF-by-default flag.

GET /api/v1/mcp/anon-usage?ip=<raw-client-ip>
  → 200 {ok:true, count:<int>, ip_hash:<sha256(ip)[:16]>}

Source of truth: mcp_tool_calls — the gateway's own per-call telemetry sink
(written by flask_mcp_endpoints.py's /api/v1/mcp/track handler, which inserts
the forwarded client IP into the RAW `ip_address` column). We count today's
rows for that exact IP:

    COUNT(*) FROM mcp_tool_calls
     WHERE ip_address = %s
       AND created_at >= date_trunc('day', (now() AT TIME ZONE 'UTC'))

FAIL-OPEN, non-negotiable: on a missing ?ip, a DB connect/timeout error, or a
missing table/column, we return {ok:true, count:0} with HTTP 200 — NEVER an
error and NEVER a large count. The gateway also fail-opens (treats any
non-clean read as count 0), so a backend hiccup can never wrongly throttle the
funnel. We bound both the connect and the statement so a slow/contended DB
returns fast-as-0 rather than hanging the gateway.

Privacy: we never echo the raw IP back — only ip_hash = sha256(ip)[:16], so the
gateway can correlate/log without this endpoint reflecting the caller's address.

PERF NOTE (enable-time, do NOT run here): mcp_tool_calls today has only
idx_mcp_tool_calls_ts ON (created_at) and ix_mcp_tool_calls_session_id — there
is NO composite index on (ip_address, created_at). The created_at index alone
forces a scan of all of today's rows filtered by ip_address. This endpoint is
ONLY hit when DCHUB_ANON_DAILY_CAP is ON, so it's currently dead weight; if/when
the cap is enabled, add:

    CREATE INDEX CONCURRENTLY IF NOT EXISTS
      idx_mcp_tool_calls_ip_created ON mcp_tool_calls (ip_address, created_at);

(DDL deliberately NOT executed by this module.)
"""
import hashlib
import logging
import os

from flask import Blueprint, jsonify, request

log = logging.getLogger(__name__)

mcp_anon_usage_bp = Blueprint("mcp_anon_usage", __name__)

# Small, bounded timeouts: this read sits in the gateway's hot path. Better to
# return count:0 (fail-open) fast than to add latency to every anon MCP call.
_CONNECT_TIMEOUT_S = 3
_STATEMENT_TIMEOUT_MS = 2000


def _ip_hash(ip: str) -> str:
    """sha256(ip)[:16] — never echo the raw IP back to the caller."""
    try:
        return hashlib.sha256((ip or "").encode("utf-8")).hexdigest()[:16]
    except Exception:
        return ""


def _today_count_for_ip(ip: str) -> int:
    """Today's (UTC) mcp_tool_calls count for this exact raw IP.

    FAIL-OPEN: any error — no DSN, connect/timeout failure, missing
    table/column — returns 0 so the gateway never throttles on a backend
    hiccup. Never raises, never returns a large fallback.
    """
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not dsn:
        return 0
    conn = None
    try:
        import psycopg2

        conn = psycopg2.connect(dsn, connect_timeout=_CONNECT_TIMEOUT_S)
        with conn.cursor() as cur:
            # Bound the statement so a contended/slow DB fails open fast
            # instead of hanging the gateway's per-call read.
            try:
                cur.execute(
                    "SET LOCAL statement_timeout = %s", (_STATEMENT_TIMEOUT_MS,)
                )
            except Exception:
                # SET LOCAL needs a tx; psycopg2 opens one implicitly, but if
                # this ever no-ops we still proceed — the connect_timeout and
                # the cheap query are the real guards.
                pass
            cur.execute(
                """
                SELECT COUNT(*)
                  FROM mcp_tool_calls
                 WHERE ip_address = %s
                   AND created_at >= date_trunc('day', (now() AT TIME ZONE 'UTC'))
                """,
                (ip,),
            )
            row = cur.fetchone()
            n = int(row[0]) if row and row[0] is not None else 0
            return n if n >= 0 else 0
    except Exception as e:  # noqa: BLE001 — fail-open is the whole contract
        log.debug("mcp_anon_usage count read failed (fail-open 0): %s", e)
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@mcp_anon_usage_bp.route("/api/v1/mcp/anon-usage", methods=["GET"])
def mcp_anon_usage():
    """Return today's tool-call count for an anonymous client IP.

    ALWAYS HTTP 200 {ok:true, count:int, ip_hash:str}. Missing ?ip → count 0
    (fail-open). The raw IP is hashed in the response, never echoed.
    """
    ip = (request.args.get("ip") or "").strip()
    if not ip:
        # No IP to key on → nothing to throttle. Fail open.
        return jsonify({"ok": True, "count": 0, "ip_hash": ""}), 200

    count = _today_count_for_ip(ip)
    return jsonify({"ok": True, "count": count, "ip_hash": _ip_hash(ip)}), 200


def register(app):
    app.register_blueprint(mcp_anon_usage_bp)
    return True
