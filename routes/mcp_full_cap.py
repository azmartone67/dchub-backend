"""
routes/mcp_full_cap.py — durable daily full-answer cap counter for the MCP server.

The MCP server (dchub-mcp-server, server.mjs) hands out a limited number of
FULL (untrimmed) tool answers per identity per day before falling back to
trial previews. Its counter used to live in worker memory, so a redeploy /
isolate rotation reset every caller's cap. These two tiny endpoints persist
the count in Postgres so the cap survives restarts.

Table (already exists in Neon — created manually; safe_db skips DDL so we
NEVER attempt CREATE here):
    mcp_full_answer_counts(
        identity   TEXT,
        tool       TEXT,
        day        DATE,
        n          INT DEFAULT 0,
        updated_at TIMESTAMPTZ DEFAULT now(),
        PRIMARY KEY (identity, tool, day))

Routes:
  POST /api/v1/mcp/full-cap/consume   {identity, tool, cap} → upsert n+1
  GET  /api/v1/mcp/full-cap/peek      ?identity=&tool=&cap=  → read-only n

Both respond {ok, n, remaining, exceeded} and stay tiny (the MCP worker calls
this on the hot path). On ANY DB failure they return {ok: false} with HTTP
200 — the MCP server fails OPEN and this endpoint must never 500-storm.

No auth BY DESIGN: `identity` is a hash/key the caller already holds (the
MCP server derives it server-side). Worst-case abuse is someone burning the
daily cap for an identity they already know — acceptable; there is nothing
to read here beyond a counter.
"""
from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

mcp_full_cap_bp = Blueprint("mcp_full_cap", __name__)

_MAX_LEN = 200

_CONSUME_SQL = """
INSERT INTO mcp_full_answer_counts (identity, tool, day, n)
VALUES (%s, %s, CURRENT_DATE, 1)
ON CONFLICT (identity, tool, day)
DO UPDATE SET n = mcp_full_answer_counts.n + 1, updated_at = now()
RETURNING n;
"""

_PEEK_SQL = """
SELECT n FROM mcp_full_answer_counts
WHERE identity = %s AND tool = %s AND day = CURRENT_DATE;
"""


def _validated(identity, tool, cap):
    """Return (identity, tool, cap:int) or (None, None, error_str)."""
    if not isinstance(identity, str) or not identity.strip() \
            or len(identity) > _MAX_LEN:
        return None, None, "identity must be a non-empty string <= 200 chars"
    if not isinstance(tool, str) or not tool.strip() or len(tool) > _MAX_LEN:
        return None, None, "tool must be a non-empty string <= 200 chars"
    # Accept a true int (JSON body) or an all-digits string (GET query param).
    # Reject bools (masquerade as ints) and floats (int() would silently
    # truncate 5.9 to 5) — both are caller bugs worth a loud 400.
    if isinstance(cap, bool):
        return None, None, "cap must be an integer 0-1000"
    if isinstance(cap, int):
        cap_i = cap
    elif isinstance(cap, str) and cap.strip().isdigit():
        cap_i = int(cap.strip())
    else:
        return None, None, "cap must be an integer 0-1000"
    if not (0 <= cap_i <= 1000):
        return None, None, "cap must be an integer 0-1000"
    return identity.strip(), tool.strip(), cap_i


def _shape(n: int, cap: int) -> dict:
    return {"ok": True, "n": n,
            "remaining": max(0, cap - n),
            "exceeded": n > cap}


@mcp_full_cap_bp.route("/api/v1/mcp/full-cap/consume", methods=["POST"])
def full_cap_consume():
    body = request.get_json(silent=True) or {}
    identity, tool, cap = _validated(
        body.get("identity"), body.get("tool"), body.get("cap"))
    if identity is None:
        return jsonify({"ok": False, "error": cap}), 400
    try:
        # Pooled connection (main.py helper) — never open raw per-request conns.
        from main import get_pg_connection, return_pg_connection
        conn = get_pg_connection(retries=1)
        try:
            cur = conn.cursor()
            cur.execute(_CONSUME_SQL, (identity, tool))
            n = int(cur.fetchone()[0])
            conn.commit()  # return_pg_connection rollbacks — commit explicitly
            return jsonify(_shape(n, cap))
        finally:
            return_pg_connection(conn)
    except Exception as e:
        # Fail open with 200 — the MCP server treats {ok:false} as "no durable
        # counter available" and falls back to its in-memory count.
        logger.warning("mcp_full_cap consume failed: %s", e)
        return jsonify({"ok": False}), 200


@mcp_full_cap_bp.route("/api/v1/mcp/full-cap/peek", methods=["GET"])
def full_cap_peek():
    identity, tool, cap = _validated(
        request.args.get("identity"), request.args.get("tool"),
        request.args.get("cap"))
    if identity is None:
        return jsonify({"ok": False, "error": cap}), 400
    try:
        from main import get_pg_connection, return_pg_connection
        conn = get_pg_connection(retries=1)
        try:
            cur = conn.cursor()
            cur.execute(_PEEK_SQL, (identity, tool))
            row = cur.fetchone()
            n = int(row[0]) if row and row[0] is not None else 0
            return jsonify(_shape(n, cap))
        finally:
            return_pg_connection(conn)
    except Exception as e:
        logger.warning("mcp_full_cap peek failed: %s", e)
        return jsonify({"ok": False}), 200
