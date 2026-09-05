"""
enterprise_inquiry.py — r47.37 (2026-05-26).

Captures inquiries from the new /enterprise data-licensing page. The
goal is to convert 1-3 enterprise contracts at $25K-$250K/yr — the
real $1M path, not 0.05% paywall conversion.

Endpoints:
  POST /api/v1/enterprise/inquiry          public — accept inquiry, store, email admin
  GET  /api/v1/admin/enterprise/inquiries  admin — pipeline view + status
  POST /api/v1/admin/enterprise/inquiry/<id>/status  admin — update status

Schema:
  enterprise_inquiries (
    id, created_at, tier_requested, name, email, firm, use_case, notes,
    source, ip_hash, status, contacted_at, notes_admin
  )

Status flow: new → contacted → meeting_scheduled → proposal_sent →
              won | lost | nurture
"""
import os
import json
import hashlib
import datetime
import logging
from contextlib import contextmanager
from flask import Blueprint, request, jsonify

try:
    import psycopg2 as _pg
    import psycopg2.extras
except Exception:
    _pg = None

logger = logging.getLogger(__name__)
enterprise_inquiry_bp = Blueprint("enterprise_inquiry", __name__)


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    c.autocommit = True
    try: yield c
    finally: c.close()


# r-inquiry-schema-fork (2026-09-05): the DDL moved to
# util/enterprise_inquiries_schema.py, which is the UNION of this module's
# definition and routes/enterprise.py's incompatible one. They were two
# `CREATE TABLE IF NOT EXISTS enterprise_inquiries` for the same table with
# disjoint columns; IF NOT EXISTS meant the loser silently no-op'd and its
# INSERT named columns that did not exist. See that module for the full note.


def _ensure_schema():
    if not (_pg and _dsn()): return
    try:
        from util.enterprise_inquiries_schema import ensure_enterprise_inquiries
        with _conn() as c, c.cursor() as cur:
            ensure_enterprise_inquiries(cur)
    except Exception as e:
        logger.warning(f"[enterprise_inquiry] schema init failed: {e}")


def _is_admin(req):
    provided = req.headers.get("X-Admin-Key") or req.headers.get("X-Internal-Key")
    if not provided: return False
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("DCHUB_INTERNAL_KEY") or "")
    if expected and provided == expected:
        return True
    try:
        from internal_auth import is_valid_internal_key
        return bool(is_valid_internal_key(provided))
    except Exception:
        return False


def _ip_hash(req):
    ip = (req.headers.get("CF-Connecting-IP")
          or req.headers.get("X-Forwarded-For", "").split(",")[0].strip()
          or req.remote_addr or "0.0.0.0")
    return hashlib.sha256(ip.encode()).hexdigest()[:16]


def _notify_admin(inquiry: dict):
    """Fire a Resend email to the admin inbox. Fire-and-forget."""
    try:
        import urllib.request as _req
        api_key = os.environ.get("RESEND_API_KEY", "")
        admin_to = os.environ.get("ADMIN_INBOX_EMAIL", "azmartone@gmail.com")
        if not api_key:
            return
        body_html = (
            f"<h2>🎯 New enterprise inquiry — {inquiry.get('firm','?')}</h2>"
            f"<p><b>Tier:</b> {inquiry.get('tier_requested','?')}</p>"
            f"<p><b>Name:</b> {inquiry.get('name','?')}<br>"
            f"<b>Email:</b> <a href='mailto:{inquiry.get('email','')}'>{inquiry.get('email','?')}</a><br>"
            f"<b>Use case:</b> {inquiry.get('use_case','?')}</p>"
            f"<p><b>Notes:</b><br>{(inquiry.get('notes') or '—').replace(chr(10), '<br>')}</p>"
            f"<p style='color:#666;font-size:12px'>"
            f"Reply directly to start the conversation. "
            f"Full pipeline at https://dchub.cloud/admin/partnerships/review</p>"
        )
        payload = json.dumps({
            "from":    "DC Hub <press@dchub.cloud>",
            "to":      [admin_to],
            "subject": f"🎯 Enterprise inquiry — {inquiry.get('firm','?')} ({inquiry.get('tier_requested','?')})",
            "html":    body_html,
            "reply_to": inquiry.get("email") or admin_to,
        }).encode()
        req = _req.Request("https://api.resend.com/emails", data=payload, headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type":  "application/json",
        })
        with _req.urlopen(req, timeout=8) as _r:
            _r.read()
    except Exception as e:
        logger.warning(f"[enterprise_inquiry] notify failed: {e}")


@enterprise_inquiry_bp.route("/api/v1/enterprise/inquiry",
                              methods=["POST"], strict_slashes=False)
def submit_inquiry():
    """Public — accept an inquiry from /enterprise. Stores + emails admin."""
    _ensure_schema()
    data = request.get_json(silent=True) or {}
    name  = (data.get("name") or "").strip()[:200]
    email = (data.get("email") or "").strip()[:200].lower()
    firm  = (data.get("firm") or "").strip()[:200]
    if not (name and email and firm):
        return jsonify({"ok": False, "error": "name, email, firm required"}), 400
    if "@" not in email or "." not in email.split("@")[-1]:
        return jsonify({"ok": False, "error": "invalid email"}), 400

    inquiry = {
        "tier_requested": (data.get("tier") or "unspecified").strip()[:50].lower(),
        "name":           name,
        "email":          email,
        "firm":           firm,
        "use_case":       (data.get("use_case") or "").strip()[:100],
        "notes":          (data.get("notes") or "").strip()[:2000],
        "source":         (data.get("source") or "enterprise_page").strip()[:50],
        "ip_hash":        _ip_hash(request),
    }

    new_id = None
    _store_err = None
    if _pg and _dsn():
        try:
            with _conn() as c, c.cursor() as cur:
                cur.execute("""
                    INSERT INTO enterprise_inquiries
                        (tier_requested, name, email, firm, use_case, notes,
                         source, ip_hash)
                    VALUES (%(tier_requested)s, %(name)s, %(email)s, %(firm)s,
                            %(use_case)s, %(notes)s, %(source)s, %(ip_hash)s)
                    RETURNING id
                """, inquiry)
                new_id = int(cur.fetchone()[0])
        except Exception as e:
            _store_err = str(e)[:200]
            logger.error(f"[enterprise_inquiry] insert failed: {e}")

    # Fire admin email asynchronously — don't block the response
    _notified = False
    try:
        _notify_admin(inquiry)
        _notified = True
    except Exception as e:
        logger.error(f"[enterprise_inquiry] admin notify failed: {e}")

    # r-inquiry-schema-fork (2026-09-05): this returned {"ok": true, ...} 201
    # and "We'll be in touch within 24h" even when the INSERT raised AND the
    # admin email raised — the submitter was told they had been heard while
    # the lead went nowhere, and the only trace was a log line nobody reads.
    # That is the same failure shape as the PDF report silently dropping a
    # market: not a missing answer, a WRONG one. The schema fork above is
    # exactly the bug that made the INSERT raise, so this endpoint has been
    # capable of losing a live enterprise lead in silence.
    #
    # A lead is captured if EITHER path worked: the row is the durable copy,
    # the admin email is the one a human actually acts on. Only when both
    # fail is the submission genuinely lost, and then the caller must be told
    # so they can use another channel rather than wait for a reply.
    if new_id is None and not _notified:
        return jsonify({
            "ok":      False,
            "error":   "storage_failed",
            "detail":  _store_err or "no storage path available",
            "message": ("We could not record your inquiry. Please email "
                        "hello@dchub.cloud directly — sorry about that."),
        }), 503

    return jsonify({
        "ok":       True,
        "id":       new_id,
        # Honest per-path result: a monitor can see a half-captured lead
        # instead of reading 201 as "both worked".
        "stored":   new_id is not None,
        "notified": _notified,
        "message":  "We'll be in touch within 24h.",
    }), 201


@enterprise_inquiry_bp.route("/api/v1/admin/enterprise/inquiries",
                              methods=["GET"], strict_slashes=False)
def list_inquiries():
    """Admin — pipeline view sorted by created_at DESC."""
    if not _is_admin(request):
        return jsonify({"error": "unauthorized"}), 401
    if not (_pg and _dsn()):
        return jsonify({"error": "no_db", "inquiries": []}), 503
    try:
        status_filter = (request.args.get("status") or "").strip().lower()
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if status_filter:
                cur.execute("""
                    SELECT * FROM enterprise_inquiries
                     WHERE status = %s
                     ORDER BY created_at DESC LIMIT 200
                """, (status_filter,))
            else:
                cur.execute("""
                    SELECT * FROM enterprise_inquiries
                     ORDER BY created_at DESC LIMIT 200
                """)
            rows = [dict(r) for r in cur.fetchall()]
            # Stringify timestamps
            for r in rows:
                for k in ("created_at", "contacted_at"):
                    if r.get(k): r[k] = r[k].isoformat()
            # Pipeline counts by status
            cur.execute("""
                SELECT status, COUNT(*) FROM enterprise_inquiries
                 GROUP BY status
            """)
            by_status = {r[0]: int(r[1]) for r in cur.fetchall()}
        return jsonify({
            "count":      len(rows),
            "by_status":  by_status,
            "inquiries":  rows,
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500


@enterprise_inquiry_bp.route("/api/v1/admin/enterprise/inquiry/<int:inq_id>/status",
                              methods=["POST"], strict_slashes=False)
def update_status(inq_id):
    """Admin — move an inquiry through the pipeline.

    Body: {"status": "contacted|meeting_scheduled|proposal_sent|won|lost|nurture",
           "notes_admin": "optional"}"""
    if not _is_admin(request):
        return jsonify({"error": "unauthorized"}), 401
    if not (_pg and _dsn()):
        return jsonify({"error": "no_db"}), 503

    data = request.get_json(silent=True) or {}
    new_status = (data.get("status") or "").strip().lower()
    allowed = ("new", "contacted", "meeting_scheduled",
               "proposal_sent", "won", "lost", "nurture")
    if new_status not in allowed:
        return jsonify({"error": f"status must be one of {allowed}"}), 400

    notes_admin = (data.get("notes_admin") or "").strip()[:2000]
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                UPDATE enterprise_inquiries
                   SET status      = %s,
                       notes_admin = COALESCE(NULLIF(%s,''), notes_admin),
                       contacted_at = CASE
                         WHEN %s IN ('contacted','meeting_scheduled','proposal_sent','won','lost','nurture')
                              AND contacted_at IS NULL THEN NOW()
                         ELSE contacted_at
                       END
                 WHERE id = %s
            """, (new_status, notes_admin, new_status, inq_id))
            n = cur.rowcount
        if n == 0:
            return jsonify({"error": "not_found"}), 404
        return jsonify({"ok": True, "id": inq_id, "status": new_status}), 200
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500
