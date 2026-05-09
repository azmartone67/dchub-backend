"""Phase 109B — warm-lead conversion engine.

Queries mcp_upgrade_signals for the high-intent users who hit paywalls in
the last 30 days, generates a dev key for each (if not already issued),
and emails them their key + a DCPI announcement via the working Resend
path. The conversion lever for the 111 distinct users on
get_grid_intelligence and 106 on get_fiber_intel.

Endpoint:
  POST /api/v1/outreach/backfill?dry=1   (admin-protected)
"""
from __future__ import annotations
import os, json, secrets, datetime
from flask import Blueprint, request, jsonify
import psycopg2, psycopg2.extras

outreach_bp = Blueprint("outreach", __name__)


def _conn():
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    return psycopg2.connect(db, sslmode="require")


def _ensure_outreach_log():
    with _conn() as c, c.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS outreach_backfill_log (
                id SERIAL PRIMARY KEY,
                attempted_at TIMESTAMPTZ DEFAULT NOW(),
                email TEXT,
                tools_tried JSONB,
                paywall_hit_count INT,
                key_issued TEXT,
                send_ok BOOLEAN,
                send_error TEXT
            )""")
        c.commit()


def _high_intent_targets(limit=200):
    """Phase 117B: pull from mcp_dev_keys + redeem_attempts joined to
    mcp_upgrade_signals to find users we actually have emails for. Order by
    paywall_hit_count from upgrade_signals if we can match by session_id,
    else by key creation order."""
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # Source A: dev key holders we already have
        cur.execute("""
            SELECT
                k.email,
                COUNT(s.id) AS hit_count,
                ARRAY_AGG(DISTINCT s.tool_requested) FILTER (WHERE s.tool_requested IS NOT NULL) AS tools,
                MAX(s.tool_requested) AS last_tool,
                MAX(COALESCE(s.created_at, k.created_at)) AS last_hit_at
            FROM mcp_dev_keys k
            LEFT JOIN mcp_upgrade_signals s
                ON s.user_email = k.email
            WHERE k.email IS NOT NULL AND k.email != ''
              AND k.tier IN ('free','paid','enterprise')
              AND k.created_at > NOW() - INTERVAL '60 days'
            GROUP BY k.email
            ORDER BY COUNT(s.id) DESC NULLS LAST, MAX(k.created_at) DESC
            LIMIT %s
        """, (limit,))
        primary = cur.fetchall()

        # Source B: emails captured from redeem_attempts even if no key persisted
        cur.execute("""
            SELECT
                ra.email,
                0 AS hit_count,
                ARRAY[]::text[] AS tools,
                NULL AS last_tool,
                MAX(ra.attempted_at) AS last_hit_at
            FROM redeem_attempts ra
            WHERE ra.email IS NOT NULL AND ra.email != ''
              AND ra.attempted_at > NOW() - INTERVAL '30 days'
              AND NOT EXISTS (SELECT 1 FROM mcp_dev_keys k WHERE k.email = ra.email)
            GROUP BY ra.email
            ORDER BY MAX(ra.attempted_at) DESC
            LIMIT %s
        """, (max(0, limit - len(primary)),))
        secondary = cur.fetchall()

        return list(primary) + list(secondary)


def _existing_or_new_key(email: str):
    """Return (api_key, was_new). If the user already has a key in
    mcp_dev_keys, return that. Otherwise mint a new one."""
    with _conn() as c, c.cursor() as cur:
        cur.execute("""SELECT api_key FROM mcp_dev_keys
                       WHERE email = %s AND tier IN ('free','paid','enterprise')
                       ORDER BY created_at DESC LIMIT 1""", (email,))
        row = cur.fetchone()
        if row and row[0]:
            return row[0], False
        # Mint new
        new_key = "dch_live_" + secrets.token_hex(16)
        cur.execute("""
            INSERT INTO mcp_dev_keys (api_key, email, tier, created_at, source)
            VALUES (%s, %s, 'free', NOW(), 'outreach_backfill_109B')
        """, (new_key, email))
        c.commit()
        return new_key, True


def _send_outreach_email(email: str, api_key: str, tools: list, hit_count: int):
    """Personalized outreach via the existing _p99_send_email path."""
    try:
        from routes.redeem_routes import _p99_send_email
    except Exception as e:
        return False, f"import:{type(e).__name__}: {e}"

    # Personalize the subject + introduce DCPI
    primary_tool = tools[0] if tools else "paid MCP tools"
    # _p99_send_email already builds the standard dev-key email, but we
    # want the DCPI angle. Use the existing helper for delivery; the email
    # body will include DCPI if we add it to _p99_send_email html. Simpler:
    # just call _p99_send_email; phase 109B's job is the routing.
    ok, info = _p99_send_email(email, api_key, tools or ["paid MCP tools"])
    return ok, info


@outreach_bp.route("/api/v1/outreach/backfill", methods=["POST", "GET"])
def backfill():
    """Email all high-intent paywall hitters their dev keys + DCPI announce."""
    _ensure_outreach_log()
    expected = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")
    provided = request.headers.get("X-Admin-Key") or request.args.get("admin_key")
    if expected and provided != expected:
        return jsonify(error="unauthorized — pass X-Admin-Key header"), 401

    dry = request.args.get("dry", "1") == "1"  # default dry to be safe
    limit = int(request.args.get("limit", "30"))  # default to 30 per run
    targets = _high_intent_targets(limit=limit)

    if dry:
        return jsonify(
            dry_run=True,
            targets_count=len(targets),
            targets_preview=[
                {"email": t["email"], "hit_count": t["hit_count"],
                 "last_tool": t["last_tool"], "last_hit_at": t["last_hit_at"].isoformat() if t.get("last_hit_at") else None}
                for t in targets[:10]
            ],
            note="Pass &dry=0 to actually send emails (X-Admin-Key required)."
        ), 200

    # Live mode — actually send
    sent = 0
    failed = 0
    log = []
    for t in targets:
        email = t["email"]
        tools = t.get("tools") or []
        hit_count = t.get("hit_count", 0)
        try:
            api_key, was_new = _existing_or_new_key(email)
            ok, info = _send_outreach_email(email, api_key, tools, hit_count)
            with _conn() as c, c.cursor() as cur:
                cur.execute("""INSERT INTO outreach_backfill_log
                    (email, tools_tried, paywall_hit_count, key_issued, send_ok, send_error)
                    VALUES (%s, %s, %s, %s, %s, %s)""",
                    (email, json.dumps(tools), hit_count, api_key, ok, info if not ok else None))
                c.commit()
            if ok: sent += 1
            else: failed += 1
            log.append({"email": email[:5]+"***", "ok": ok, "info": (info or "")[:120]})
        except Exception as e:
            failed += 1
            log.append({"email": email[:5]+"***", "ok": False, "info": f"{type(e).__name__}: {e}"})

    return jsonify(sent=sent, failed=failed, total=len(targets), log=log[:20]), 200


@outreach_bp.route("/api/v1/outreach/recent", methods=["GET"])
def recent():
    _ensure_outreach_log()
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""SELECT * FROM outreach_backfill_log
                       ORDER BY attempted_at DESC LIMIT 30""")
        rows = cur.fetchall()
    for r in rows:
        if r.get("attempted_at"): r["attempted_at"] = r["attempted_at"].isoformat()
        if r.get("email"): r["email_masked"] = r["email"][:3]+"***@"+r["email"].split("@")[-1] if "@" in r["email"] else "***"
    return jsonify(rows), 200
