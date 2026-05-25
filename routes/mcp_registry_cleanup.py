"""
mcp_registry_cleanup.py — mark dead-URL MCP registries as defunct.

Phase ZZZZZ-round47.9 (2026-05-25). Three of the 11 registries in our
tracker have stale submit URLs that 404 (verified by manual probe via
Chrome MCP):

  - MCPHub (mcphub.io/submit → 404)
  - MCP Hive (mcphive.com/submit → "This Page Does Not Exist")
  - ToolHive (toolhive.io/submit → redirects to compliancehive.eu, unrelated)

Marking these as `defunct` in the in-memory registry stops them from
showing up as "queued" in /api/v1/admin/outreach/mcp-registry/status,
which was creating misleading "we have 7 pending submissions" reports
when 3 of those 7 are dead targets.

Provides:
  POST /api/v1/admin/outreach/mcp-registry/mark-defunct
       body: {"keys": ["mcphub", "mcphive", "toolhive"], "reason": "..."}
"""
import os
import datetime
from flask import Blueprint, request, jsonify

mcp_registry_cleanup_bp = Blueprint("mcp_registry_cleanup", __name__)


# Curated default — the 3 confirmed-dead targets from r47 manual probe.
DEFAULT_DEFUNCT_KEYS = ["mcphub", "mcphive", "toolhive"]


def _is_admin(req):
    expected = os.environ.get("DCHUB_ADMIN_KEY", "").strip()
    if not expected:
        return False
    got = req.headers.get("X-Admin-Key", "").strip()
    return got and got == expected


@mcp_registry_cleanup_bp.route("/api/v1/admin/outreach/mcp-registry/mark-defunct",
                                methods=["POST", "GET"])
def mark_defunct():
    """Mark a registry target as defunct (URL no longer valid).

    Reflective endpoint — doesn't mutate the tracker module's hardcoded
    list (that lives in routes/mcp_registry_outreach.py and is read-only
    runtime data), but it DOES write to the `mcp_registry_defunct` DB
    table that the status endpoint reads as a filter overlay.
    """
    if request.method == "POST" and not _is_admin(request):
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    keys = body.get("keys") or DEFAULT_DEFUNCT_KEYS
    reason = body.get("reason") or "URL dead per r47 manual probe (5/25/2026)"

    try:
        import psycopg2
        dsn = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
        if not dsn:
            return jsonify({"error": "no_db"}), 503
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mcp_registry_defunct (
                    key         TEXT PRIMARY KEY,
                    reason      TEXT,
                    marked_at   TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            inserted = []
            for k in keys:
                cur.execute("""
                    INSERT INTO mcp_registry_defunct (key, reason)
                    VALUES (%s, %s)
                    ON CONFLICT (key) DO UPDATE
                       SET reason = EXCLUDED.reason, marked_at = NOW()
                    RETURNING key
                """, (k, reason))
                row = cur.fetchone()
                if row:
                    inserted.append(row[0])
        conn.close()
        return jsonify({
            "ok": True,
            "marked_defunct": inserted,
            "reason": reason,
            "at": datetime.datetime.utcnow().isoformat() + "Z",
            "hint": "Status endpoint will hide these on next refresh. To restore, DELETE FROM mcp_registry_defunct WHERE key = '...';",
        }), 200
    except Exception as e:
        return jsonify({
            "error": f"{type(e).__name__}",
            "detail": str(e)[:200],
        }), 500


@mcp_registry_cleanup_bp.route("/api/v1/admin/outreach/mcp-registry/defunct-list",
                                methods=["GET"])
def list_defunct():
    """Public read of the defunct list (no auth)."""
    try:
        import psycopg2
        dsn = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
        if not dsn:
            return jsonify({"defunct": []}), 200
        conn = psycopg2.connect(dsn)
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT key, reason, marked_at FROM mcp_registry_defunct ORDER BY marked_at DESC")
                rows = cur.fetchall()
            except Exception:
                rows = []
        conn.close()
        return jsonify({
            "defunct": [
                {"key": r[0], "reason": r[1], "marked_at": r[2].isoformat() if r[2] else None}
                for r in rows
            ],
            "count": len(rows),
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)[:120], "defunct": []}), 500
