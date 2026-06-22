"""Explicit marketing opt-in capture — POST /api/v1/keys/marketing-consent (2026-06-22).

Companion to /keys/identify (which binds an email, TRANSACTIONAL only). NOTHING
previously set mcp_dev_keys.metadata->>'marketing_opt_in' — so the opt-in pool the
winback digest + broadcast read was 0 (30 keys with emails, 0 opted in, 0 digests
ever sent). This records EXPLICIT, agent-relayed consent:

  * sets metadata->>'marketing_opt_in' = 'true' (the field the digest reads), and
  * writes an audit row to opt_in_consents (email, source, ip, ua, timestamps).

Abuse guard: you can only opt-in a key you hold (api_key) OR an email ALREADY bound
to a DC Hub key — you cannot subscribe an arbitrary stranger. opt_in=false honors
withdrawal (clears the flag). Every marketing send remains suppression-gated +
carries unsubscribe downstream. Soft-fail: errors return 200 so the bind flow is
never broken.
"""
import os
from datetime import datetime, timezone

import psycopg2
from flask import Blueprint, jsonify, request

marketing_consent_bp = Blueprint("marketing_consent", __name__)


@marketing_consent_bp.route("/api/v1/keys/marketing-consent", methods=["POST"])
def marketing_consent():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()[:200]
    api_key = (data.get("api_key") or "").strip()[:100]
    opt_in = bool(data.get("opt_in") if data.get("opt_in") is not None else data.get("marketing_opt_in"))
    if not email and not api_key:
        return jsonify(ok=False, message="email or api_key required"), 400

    val = "true" if opt_in else "false"
    ip = ((request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[0]).strip()[:80]
    ua = (request.headers.get("User-Agent") or "")[:300]
    conn = None
    try:
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        conn.autocommit = False
        cur = conn.cursor()
        # Resolve the key by api_key (preferred) or an ALREADY-bound email.
        if api_key:
            cur.execute("SELECT api_key, email FROM mcp_dev_keys WHERE api_key=%s", (api_key,))
        else:
            cur.execute(
                "SELECT api_key, email FROM mcp_dev_keys WHERE lower(email)=%s "
                "ORDER BY created_at DESC LIMIT 1", (email,))
        row = cur.fetchone()
        if not row:
            conn.rollback()
            # Neutral: don't reveal whether the key/email exists.
            return jsonify(ok=True, opted_in=opt_in, message="recorded"), 200
        resolved_key, bound_email = row[0], (row[1] or email)

        cur.execute(
            "UPDATE mcp_dev_keys SET metadata = COALESCE(metadata, '{}'::jsonb) "
            "|| jsonb_build_object('marketing_opt_in', %s) WHERE api_key=%s",
            (val, resolved_key))
        if opt_in and bound_email:
            now = datetime.now(timezone.utc)
            cur.execute(
                "INSERT INTO opt_in_consents (email, source, ip, user_agent, requested_at, confirmed_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (bound_email.lower(), "mcp_bind_email", ip, ua, now, now))
        conn.commit()
        return jsonify(ok=True, opted_in=opt_in, email=bound_email,
                       message=("subscribed to the weekly digest" if opt_in else "unsubscribed")), 200
    except Exception as e:
        try:
            if conn:
                conn.rollback()
        except Exception:
            pass
        return jsonify(ok=False, message=str(e)[:200]), 200  # soft-fail: never break bind
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass
