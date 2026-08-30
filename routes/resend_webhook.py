"""
routes/resend_webhook.py — delivery-truth ingest for Resend events (2026-07-17).

welcome_email_log records send ATTEMPTS only, and the RESEND_API_KEY on
Railway is send-only, so delivery could never be confirmed. This endpoint
receives Resend's webhooks (email.sent / delivered / bounced / opened /
complained / delivery_delayed) and persists them to email_events, joined back
to sends via the resend_message_id we now store on welcome_email_log.

Setup (one click, owner): Resend dashboard -> Webhooks -> Add endpoint
  https://dchub.cloud/api/v1/webhooks/resend
Optionally paste the endpoint's signing secret into RESEND_WEBHOOK_SECRET on
Railway — signature verification (Svix scheme) then becomes ENFORCED. Without
the secret, events are stored with verified=false (still useful truth; the
payload carries nothing an attacker gains by writing, and svix_id dedupes).

POST /api/v1/webhooks/resend   <- Resend calls this; always answer fast.
GET  /api/v1/admin/email-events?email=x (X-Admin-Key) -> recent events, joined.
"""
import os
import json
import hmac
import base64
import hashlib
import logging
from flask import Blueprint, request, jsonify

logger = logging.getLogger("resend_webhook")
resend_webhook_bp = Blueprint("resend_webhook", __name__)

MAX_BODY = 64 * 1024


def _get_conn():
    """Direct psycopg2 (autocommit) — the safe_db wrapper silently skips DDL."""
    import psycopg2
    url = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL', '')
    if not url:
        raise Exception("No NEON_DATABASE_URL or DATABASE_URL set")
    conn = psycopg2.connect(url, connect_timeout=8)
    conn.autocommit = True
    return conn


def _ensure_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS email_events (
            id SERIAL PRIMARY KEY,
            svix_id TEXT UNIQUE,
            event_type TEXT,
            email TEXT,
            resend_message_id TEXT,
            subject TEXT,
            occurred_at TIMESTAMPTZ,
            verified BOOLEAN DEFAULT FALSE,
            payload TEXT,
            received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_email_events_mid "
                "ON email_events (resend_message_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_email_events_email "
                "ON email_events (lower(email))")


def _verify_svix(secret, svix_id, svix_timestamp, svix_signature, raw_body):
    """Svix HMAC-SHA256: base64(HMAC(secret', '{id}.{ts}.{body}')) where
    secret' = base64-decoded secret after the 'whsec_' prefix. The signature
    header holds space-separated 'v1,<b64>' entries. Returns bool."""
    try:
        if not (svix_id and svix_timestamp and svix_signature):
            return False
        key = secret.strip()
        if key.startswith("whsec_"):
            key = key[len("whsec_"):]
        key_bytes = base64.b64decode(key + "=" * (-len(key) % 4))
        signed = f"{svix_id}.{svix_timestamp}.".encode() + raw_body
        expect = base64.b64encode(
            hmac.new(key_bytes, signed, hashlib.sha256).digest()).decode()
        for part in svix_signature.split(" "):
            if "," in part:
                _v, sig = part.split(",", 1)
                if hmac.compare_digest(sig, expect):
                    return True
        return False
    except Exception:
        return False


@resend_webhook_bp.route('/api/v1/webhooks/resend', methods=['POST'])
def resend_webhook():
    raw = request.get_data(cache=False, as_text=False) or b''
    if len(raw) > MAX_BODY:
        return jsonify(ok=False, error='payload_too_large'), 413

    secret = (os.environ.get('RESEND_WEBHOOK_SECRET') or '').strip()
    verified = False
    if secret:
        verified = _verify_svix(
            secret,
            request.headers.get('svix-id', ''),
            request.headers.get('svix-timestamp', ''),
            request.headers.get('svix-signature', ''),
            raw)
        if not verified:
            return jsonify(ok=False, error='bad_signature'), 401

    try:
        evt = json.loads(raw.decode('utf-8', errors='replace')) or {}
    except Exception:
        return jsonify(ok=False, error='bad_json'), 400

    data = evt.get('data') or {}
    to = data.get('to')
    email = (to[0] if isinstance(to, list) and to else to) or None
    if isinstance(email, str):
        email = email.strip().lower()[:320] or None
    occurred = data.get('created_at') or evt.get('created_at') or None

    try:
        conn = _get_conn()
        try:
            cur = conn.cursor()
            _ensure_table(cur)
            cur.execute("""
                INSERT INTO email_events
                    (svix_id, event_type, email, resend_message_id, subject,
                     occurred_at, verified, payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (svix_id) DO NOTHING
            """, (
                (request.headers.get('svix-id') or None),
                str(evt.get('type') or 'unknown')[:80],
                email,
                (str(data.get('email_id'))[:100] if data.get('email_id') else None),
                (str(data.get('subject'))[:300] if data.get('subject') else None),
                occurred,
                verified,
                raw.decode('utf-8', errors='replace')[:8000],
            ))

            # ★ Promote the outreach draft this event belongs to. Until
            # 2026-08-30 ai_lab_outreach_drafts recorded status='sent' off a
            # bare HTTP 200 — which Resend also returns for a SUPPRESSED
            # recipient it never attempts to deliver. Six of nine AI-lab
            # targets were suppressed, so ~30 of 45 "sent" drafts never left.
            # The webhook is the only thing that knows what actually happened,
            # so it is the only thing allowed to say so.
            _state = {
                'email.delivered':       'delivered',
                'email.bounced':         'bounced',
                'email.complained':      'complained',
                'email.delivery_delayed': 'delayed',
            }.get(str(evt.get('type') or ''))
            _mid = (str(data.get('email_id'))[:100] if data.get('email_id') else None)
            if _state and _mid:
                try:
                    # ★ Never downgrade: a delayed/bounced event arriving after
                    # a delivered one must not overwrite the delivery. Only
                    # 'submitted' (or nothing) is promotable.
                    cur.execute("""
                        UPDATE ai_lab_outreach_drafts
                           SET delivery_state = %s, delivery_state_at = NOW()
                         WHERE resend_id = %s
                           AND COALESCE(delivery_state, 'submitted') <> 'delivered'
                    """, (_state, _mid))
                except Exception as _pe:
                    # A missing column or table is not a reason to 500 at
                    # Resend — the event itself is already stored above.
                    logger.debug("resend_webhook: draft promote skipped: %s",
                                 str(_pe)[:120])
            cur.close()
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as e:
        # Never make Resend retry-storm us over a DB blip — log and ack.
        logger.warning("resend_webhook: persist failed (%s): %s",
                       evt.get('type'), str(e)[:160])
    return jsonify(ok=True), 200


@resend_webhook_bp.route('/api/v1/admin/email-events', methods=['GET'])
def admin_email_events():
    provided = request.headers.get('X-Admin-Key') or request.args.get('admin_key')
    if provided != os.environ.get('DCHUB_ADMIN_KEY', ''):
        return jsonify({'error': 'unauthorized', 'hint': 'X-Admin-Key required'}), 401
    email = (request.args.get('email') or '').strip().lower()
    try:
        limit = max(1, min(int(request.args.get('limit', 50)), 200))
    except Exception:
        limit = 50
    try:
        conn = _get_conn()
        try:
            cur = conn.cursor()
            _ensure_table(cur)
            if email:
                cur.execute("""
                    SELECT event_type, email, resend_message_id, subject,
                           occurred_at::text, verified, received_at::text
                      FROM email_events
                     WHERE lower(email) = %s
                     ORDER BY received_at DESC LIMIT %s""", (email, limit))
            else:
                cur.execute("""
                    SELECT event_type, email, resend_message_id, subject,
                           occurred_at::text, verified, received_at::text
                      FROM email_events
                     ORDER BY received_at DESC LIMIT %s""", (limit,))
            rows = [{'event_type': r[0], 'email': r[1],
                     'resend_message_id': r[2], 'subject': r[3],
                     'occurred_at': r[4], 'verified': r[5],
                     'received_at': r[6]} for r in cur.fetchall()]
            cur.close()
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return jsonify(ok=True, count=len(rows), events=rows)
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500
