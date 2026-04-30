"""
DC Hub — Flask MCP key validation + telemetry endpoints (Neon Postgres)
─────────────────────────────────────────────────────────────────────────
Drop into the Railway-deployed Flask backend, register the blueprint:

    from flask_mcp_endpoints import mcp_bp
    app.register_blueprint(mcp_bp)

Required env (Railway):
    NEON_DATABASE_URL    — postgres://… connection string (use the Neon pooler URL)
    DCHUB_INTERNAL_KEY   — must match the value in the patched server.mjs

Endpoints:
    POST /api/v1/keys/validate   {api_key} → {valid, tier, developer_id, email}
    POST /api/v1/mcp/track       {tool, params, platform, api_key, tier,
                                  session_id, status, duration_ms, timestamp}
    GET  /api/v1/mcp/stats?days=N  (internal) → 7d rollup of tool calls + funnel

All endpoints require X-Internal-Key matching DCHUB_INTERNAL_KEY.

Dependencies (add to requirements.txt):
    psycopg[binary,pool]>=3.2
"""

import os
import json
from datetime import datetime, timezone
from functools import wraps

from flask import Blueprint, request, jsonify
import psycopg
from contextlib import contextmanager

mcp_bp = Blueprint("mcp_bp", __name__)

NEON_URL     = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
INTERNAL_KEY = os.environ.get("DCHUB_INTERNAL_KEY", "dchub-internal-sync-2026")


@contextmanager
def _conn_ctx():
    conn = psycopg.connect(NEON_URL, autocommit=True)
    try:
        yield conn
    finally:
        conn.close()

class _PoolShim:
    def connection(self):
        return _conn_ctx()
_pool = _PoolShim()

if not NEON_URL:
    raise RuntimeError("NEON_DATABASE_URL (or DATABASE_URL) must be set for flask_mcp_endpoints")

# Connection pool — small and short-lived, suitable for Railway's container model.
# If you add Cloudflare Hyperdrive, point NEON_DATABASE_URL at the Hyperdrive URL.
,
    kwargs={"autocommit": True},
)


def _require_internal(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if request.headers.get("X-Internal-Key") != INTERNAL_KEY:
            return jsonify({"error": "forbidden"}), 403
        return fn(*args, **kwargs)
    return wrapper


# ── POST /api/v1/keys/validate ────────────────────────────────────────────

@mcp_bp.post("/api/v1/keys/validate")
@_require_internal
def validate_key():
    body    = request.get_json(silent=True) or {}
    api_key = (body.get("api_key") or "").strip()
    if not api_key:
        return jsonify({"valid": False, "tier": "free"}), 200

    with _pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT developer_id, email, tier, status
               FROM mcp_dev_keys WHERE api_key = %s""",
            (api_key,),
        )
        row = cur.fetchone()

    if not row or row[3] != "active":
        return jsonify({"valid": False, "tier": "free"}), 200

    # Lazy last_used update — best effort, no need to block the response on it
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE mcp_dev_keys SET last_used_at = NOW() WHERE api_key = %s",
                (api_key,),
            )
    except Exception:
        pass

    return jsonify({
        "valid":        True,
        "tier":         row[2] or "free",
        "developer_id": row[0],
        "email":        row[1],
    }), 200


# ── POST /api/v1/mcp/track ────────────────────────────────────────────────

@mcp_bp.post("/api/v1/mcp/track")
@_require_internal
def track_tool_call():
    body = request.get_json(silent=True) or {}
    tool = body.get("tool")
    if not tool:
        return jsonify({"ok": False, "error": "missing tool"}), 200

    ts = body.get("timestamp")
    try:
        ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else datetime.now(timezone.utc)
    except Exception:
        ts_dt = datetime.now(timezone.utc)

    params = body.get("params")
    if params is not None and not isinstance(params, str):
        params = json.dumps(params, default=str)

    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO mcp_call_log
                     (timestamp, tool, params, platform, api_key, tier,
                      session_id, status, duration_ms)
                   VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s)""",
                (
                    ts_dt,
                    tool,
                    params,
                    body.get("platform"),
                    body.get("api_key"),
                    body.get("tier"),
                    body.get("session_id"),
                    body.get("status"),
                    body.get("duration_ms"),
                ),
            )
    except Exception as e:
        # Never block the MCP server on logging failures. Log and return 200 ok=false.
        return jsonify({"ok": False, "error": str(e)}), 200

    return jsonify({"ok": True}), 200


# ── GET /api/v1/mcp/stats — for our own dashboard / triage ────────────────

@mcp_bp.get("/api/v1/mcp/stats")
@_require_internal
def mcp_stats():
    try:
        days = max(1, min(int(request.args.get("days", "7")), 90))
    except ValueError:
        days = 7

    out = {"window_days": days}

    with _pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT date_trunc('day', timestamp)::date AS day,
                      platform, COUNT(*) AS n
               FROM mcp_call_log
               WHERE timestamp >= NOW() - make_interval(days => %s)
               GROUP BY day, platform
               ORDER BY day DESC, n DESC""",
            (days,),
        )
        out["by_day_platform"] = [
            {"day": str(r[0]), "platform": r[1], "n": r[2]} for r in cur.fetchall()
        ]

        cur.execute(
            """SELECT tool,
                      COUNT(*)::int                                       AS n,
                      AVG(duration_ms)::int                                AS avg_ms,
                      COUNT(*) FILTER (WHERE status='error')::int          AS errors,
                      COUNT(*) FILTER (WHERE status='blocked_paid_only')::int AS upgrade_blocks,
                      COUNT(DISTINCT api_key)::int                         AS distinct_devs
               FROM mcp_call_log
               WHERE timestamp >= NOW() - make_interval(days => %s)
               GROUP BY tool
               ORDER BY n DESC""",
            (days,),
        )
        out["by_tool"] = [
            {
                "tool": r[0], "n": r[1], "avg_ms": r[2],
                "errors": r[3], "upgrade_blocks": r[4], "distinct_devs": r[5],
            }
            for r in cur.fetchall()
        ]

        cur.execute(
            """SELECT
                 COUNT(*) FILTER (WHERE api_key IS NOT NULL)::int      AS keyed_calls,
                 COUNT(DISTINCT api_key)                                AS keyed_devs,
                 COUNT(DISTINCT session_id)                             AS sessions,
                 COUNT(*)::int                                          AS tool_calls,
                 COUNT(*) FILTER (WHERE status='blocked_paid_only')::int AS paid_block_events
               FROM mcp_call_log
               WHERE timestamp >= NOW() - make_interval(days => %s)""",
            (days,),
        )
        r = cur.fetchone() or (0, 0, 0, 0, 0)
        out["funnel"] = {
            "keyed_calls":       r[0] or 0,
            "keyed_devs":        r[1] or 0,
            "sessions":          r[2] or 0,
            "tool_calls":        r[3] or 0,
            "paid_block_events": r[4] or 0,
        }

        cur.execute(
            "SELECT tier, COUNT(*)::int FROM mcp_dev_keys WHERE status='active' GROUP BY tier ORDER BY tier"
        )
        out["keys_by_tier"] = [{"tier": r[0], "n": r[1]} for r in cur.fetchall()]

    return jsonify(out), 200


# ── POST /api/v1/dev-signup — Self-serve free dev key issuance (PUBLIC) ────

@mcp_bp.post("/api/v1/dev-signup")
def dev_signup():
    import secrets
    body  = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    if not email or "@" not in email or len(email) > 254:
        return jsonify({"error": "valid email required"}), 400

    api_key      = f"dch_live_{secrets.token_hex(16)}"
    developer_id = f"dev_{secrets.token_hex(8)}"

    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT api_key FROM mcp_dev_keys WHERE email=%s AND status=%s LIMIT 1",
                (email, "active"),
            )
            existing = cur.fetchone()
            if existing:
                return jsonify({
                    "api_key": existing[0], "tier": "free", "email": email,
                    "is_new": False, "header": "X-API-Key",
                    "docs": "https://dchub.cloud/ai",
                    "upgrade_url": "https://dchub.cloud/ai#pricing",
                }), 200
            cur.execute(
                """INSERT INTO mcp_dev_keys
                     (api_key, developer_id, email, tier, status, metadata)
                   VALUES (%s, %s, %s, %s, %s, %s::jsonb)""",
                (api_key, developer_id, email, "free", "active",
                 '{"source":"dev-signup-form"}'),
            )
    except Exception as e:
        return jsonify({"error": "key issuance failed", "detail": str(e)}), 500

    return jsonify({
        "api_key": api_key, "tier": "free", "email": email,
        "is_new": True, "header": "X-API-Key",
        "docs": "https://dchub.cloud/ai",
        "upgrade_url": "https://dchub.cloud/ai#pricing",
    }), 200


# ── GET /api/v1/mcp/funnel — Public aggregate stats for the dashboard ─────

@mcp_bp.get("/api/v1/mcp/funnel")
def mcp_funnel():
    out = {}
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute("""SELECT COUNT(*) FROM mcp_tool_calls
                           WHERE created_at >= NOW() - INTERVAL %s""",
                        ("7 days",))
            out["tool_calls_7d"] = cur.fetchone()[0]

            cur.execute("""SELECT COUNT(*) FROM mcp_upgrade_signals
                           WHERE created_at >= NOW() - INTERVAL %s""",
                        ("7 days",))
            out["upgrade_signals_7d"] = cur.fetchone()[0]

            cur.execute("""SELECT COUNT(*) FROM mcp_conversions
                           WHERE created_at >= NOW() - INTERVAL %s""",
                        ("30 days",))
            out["conversions_30d"] = cur.fetchone()[0]

            cur.execute("""SELECT tier, COUNT(*) FROM mcp_dev_keys
                           WHERE status=%s GROUP BY tier""", ("active",))
            out["keys_by_tier"] = {r[0]: r[1] for r in cur.fetchall()}

            cur.execute("""SELECT tool_requested, COUNT(*) AS n
                           FROM mcp_upgrade_signals
                           WHERE created_at >= NOW() - INTERVAL %s
                           GROUP BY tool_requested ORDER BY n DESC LIMIT 10""",
                        ("30 days",))
            out["top_signal_tools_30d"] = [{"tool": r[0], "n": r[1]} for r in cur.fetchall()]

            cur.execute("""SELECT tool_name, COUNT(*) AS n,
                                  COUNT(DISTINCT ip_address) AS users
                           FROM mcp_tool_calls
                           WHERE tool_name IN (%s,%s,%s,%s,%s)
                             AND created_at >= NOW() - INTERVAL %s
                           GROUP BY tool_name ORDER BY n DESC""",
                        ("analyze_site","compare_sites","get_grid_intelligence",
                         "get_dchub_recommendation","get_fiber_intel","30 days"))
            out["paid_tool_demand_30d"] = [
                {"tool": r[0], "calls": r[1], "users": r[2]} for r in cur.fetchall()
            ]
    except Exception as e:
        out["error"] = str(e)
    return jsonify(out), 200


# ── GET /api/v1/mcp/dashboard — serve the dashboard HTML through Flask ────

@mcp_bp.get("/api/v1/mcp/dashboard")
def mcp_dashboard():
    """Serve the dashboard HTML directly so it goes through Cloudflare's /api/* route."""
    from flask import Response
    try:
        with open("static/mcp-dashboard.html", "r") as f:
            return Response(f.read(), mimetype="text/html")
    except FileNotFoundError:
        return Response("dashboard not found", status=404)
