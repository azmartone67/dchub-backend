"""
redeem_diagnostic.py — diagnose why email-sending in redeem flow fails.

This module DOES NOT modify redeem_routes.py directly. Instead it:
  1. Tracks attempts in a new redeem_attempts table
  2. Provides /api/v1/redeem/diagnostic/<email> to see what happened
  3. Provides /api/v1/redeem/diagnostic/health to check email-system status

Phase 99 — debugging conversion gate failure.

To wire into the existing redeem flow, edit routes/redeem_routes.py to call
record_redeem_attempt() after the email-send block. One line. No risk.
"""

import json
import os
import socket
import smtplib
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg2 as _pg
from flask import Blueprint, jsonify, request


redeem_diagnostic_bp = Blueprint("redeem_diagnostic", __name__, url_prefix="/api/v1/redeem/diagnostic")


def _dsn(): return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""

@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try: yield c
    finally: c.close()


MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS redeem_attempts (
    id              BIGSERIAL PRIMARY KEY,
    attempted_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    session_id      TEXT,
    email           TEXT,
    ip_hash         TEXT,
    user_agent      TEXT,
    email_send_ok   BOOLEAN,
    email_send_error TEXT,
    api_key_created BOOLEAN,
    api_key_id      TEXT,
    extra           JSONB
);

CREATE INDEX IF NOT EXISTS ix_redeem_attempts_email ON redeem_attempts (email);
CREATE INDEX IF NOT EXISTS ix_redeem_attempts_at ON redeem_attempts (attempted_at DESC);
"""


def _ensure_table():
    if getattr(_ensure_table, "_done", False): return
    with _conn() as c, c.cursor() as cur:
        cur.execute(MIGRATION_SQL)
        c.commit()
    _ensure_table._done = True


def record_redeem_attempt(
    session_id, email, email_send_ok=None, email_send_error=None,
    api_key_created=None, api_key_id=None, extra=None,
):
    """Call this from inside the redeem POST handler after email send.
    Returns the attempt id, or None if logging fails (best-effort).
    """
    _ensure_table()
    import hashlib
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    ip_hash = hashlib.sha256(ip.encode("utf-8")).hexdigest()[:16] if ip else None
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                """INSERT INTO redeem_attempts
                       (session_id, email, ip_hash, user_agent,
                        email_send_ok, email_send_error,
                        api_key_created, api_key_id, extra)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                   RETURNING id""",
                (
                    session_id, email, ip_hash,
                    request.headers.get("User-Agent", "")[:500],
                    email_send_ok, str(email_send_error)[:1000] if email_send_error else None,
                    api_key_created, api_key_id,
                    json.dumps(extra or {}),
                ),
            )
            run_id = cur.fetchone()[0]
            c.commit()
            return run_id
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Diagnostic endpoints
# ---------------------------------------------------------------------------

@redeem_diagnostic_bp.route("/<string:email>", methods=["GET"])
def diagnose_email(email):
    """Look up all redeem attempts for an email, see what happened."""
    _ensure_table()
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """SELECT id, attempted_at, session_id, email_send_ok,
                      email_send_error, api_key_created, api_key_id, extra
               FROM redeem_attempts
               WHERE email = %s
               ORDER BY attempted_at DESC LIMIT 20""",
            (email,),
        )
        rows = cur.fetchall()

    if not rows:
        return jsonify(
            email=email,
            found=False,
            note="No redeem attempts recorded for this email. Either the email never reached the form submit handler, OR the redeem POST handler isn't calling record_redeem_attempt() yet."
        ), 200

    return jsonify(
        email=email,
        attempts=[
            {
                "id": r[0],
                "attempted_at": r[1].isoformat() if r[1] else None,
                "session_id": r[2],
                "email_send_ok": r[3],
                "email_send_error": r[4],
                "api_key_created": r[5],
                "api_key_id": r[6],
                "extra": r[7],
            }
            for r in rows
        ]
    ), 200


@redeem_diagnostic_bp.route("/health", methods=["GET"])
def health():
    """Test connectivity to common email services."""
    _ensure_table()

    smtp_envs = {
        "SMTP_HOST": os.environ.get("SMTP_HOST"),
        "SMTP_PORT": os.environ.get("SMTP_PORT"),
        "SMTP_USER": "[set]" if os.environ.get("SMTP_USER") else None,
        "SMTP_PASS": "[set]" if os.environ.get("SMTP_PASS") else None,
        "SENDGRID_API_KEY": "[set]" if os.environ.get("SENDGRID_API_KEY") else None,
        "MAILGUN_API_KEY": "[set]" if os.environ.get("MAILGUN_API_KEY") else None,
        "RESEND_API_KEY": "[set]" if os.environ.get("RESEND_API_KEY") else None,
        "POSTMARK_API_KEY": "[set]" if os.environ.get("POSTMARK_API_KEY") else None,
        "AWS_SES_REGION": os.environ.get("AWS_SES_REGION"),
    }

    # Reachability test for known SMTP host (best-effort)
    smtp_reachable = None
    if smtp_envs.get("SMTP_HOST"):
        try:
            host = smtp_envs["SMTP_HOST"]
            port = int(smtp_envs.get("SMTP_PORT") or 587)
            with socket.create_connection((host, port), timeout=5):
                smtp_reachable = True
        except Exception as e:
            smtp_reachable = False

    # Recent attempts summary
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """SELECT
                  COUNT(*) AS total,
                  SUM(CASE WHEN email_send_ok IS TRUE THEN 1 ELSE 0 END) AS sent_ok,
                  SUM(CASE WHEN email_send_ok IS FALSE THEN 1 ELSE 0 END) AS sent_fail,
                  SUM(CASE WHEN api_key_created IS TRUE THEN 1 ELSE 0 END) AS keys_created,
                  MAX(attempted_at) AS last_attempt
               FROM redeem_attempts
               WHERE attempted_at > NOW() - INTERVAL '24 hours'"""
        )
        total, sent_ok, sent_fail, keys, last = cur.fetchone()

    return jsonify(
        smtp_envs={k: v for k, v in smtp_envs.items() if v},
        smtp_reachable=smtp_reachable,
        attempts_24h={
            "total": int(total or 0),
            "email_sent_ok": int(sent_ok or 0),
            "email_sent_failed": int(sent_fail or 0),
            "api_keys_created": int(keys or 0),
            "last_attempt_at": last.isoformat() if last else None,
        },
        notes=[
            "If email_sent_ok is 0 but email_sent_failed > 0, look at email_send_error per attempt.",
            "If both 0 with attempts > 0, the redeem POST handler isn't logging — wire record_redeem_attempt().",
            "If smtp_envs is empty, no email service is configured — that's likely the bug.",
        ]
    ), 200


@redeem_diagnostic_bp.route("/recent", methods=["GET"])
def recent():
    """Last 20 redeem attempts across all emails (admin view)."""
    _ensure_table()
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """SELECT attempted_at, email, email_send_ok, email_send_error,
                      api_key_created, api_key_id
               FROM redeem_attempts
               ORDER BY attempted_at DESC LIMIT 20"""
        )
        rows = cur.fetchall()
    return jsonify(
        count=len(rows),
        attempts=[
            {
                "attempted_at": r[0].isoformat() if r[0] else None,
                "email_masked": (r[1].split("@")[0][:2] + "***@" + r[1].split("@")[1]) if r[1] and "@" in r[1] else r[1],
                "email_send_ok": r[2],
                "email_send_error": (r[3] or "")[:200],
                "api_key_created": r[4],
                "api_key_id": r[5],
            }
            for r in rows
        ]
    ), 200
