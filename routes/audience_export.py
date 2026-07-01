"""
audience_export.py — admin export of the free-tier user audience (2026-07-01).
==============================================================================

For upsell campaigns (e.g. Nico's Pro upsell): a one-call CSV of every FREE-tier
user with an email — first name, last name, email (+ company / tier / signup) —
with paid users and the email-suppression list excluded so the send is clean.

  GET /api/v1/admin/audience/free-users.csv           → CSV download
  GET /api/v1/admin/audience/free-users?format=json   → count + sample (preview)

Admin-gated (X-Admin-Key = DCHUB_ADMIN_KEY, or X-Internal-Key = DCHUB_INTERNAL_KEY).

"Free" = has a valid email AND is NOT on a paid plan/tier (pro / founding /
enterprise). Suppressed / unsubscribed / bounced addresses (email_suppression)
are removed. Deduped by lower(email), newest signup kept.
"""
from __future__ import annotations

import csv
import io
import logging
import os

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)
audience_export_bp = Blueprint("audience_export", __name__)

_PAID = ("pro", "founding", "enterprise")


def _admin_ok() -> bool:
    import hmac
    ak = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()
    ik = (os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
    got_a = (request.headers.get("X-Admin-Key") or request.args.get("admin_key") or "").strip()
    got_i = (request.headers.get("X-Internal-Key") or "").strip()
    if ak and got_a and hmac.compare_digest(got_a, ak):
        return True
    if ik and got_i and hmac.compare_digest(got_i, ik):
        return True
    return False


def _conn():
    try:
        from main import get_pg_connection
        return get_pg_connection()
    except Exception as e:
        logger.warning("[audience_export] no DB: %s", e)
        return None


def _return(c, error=False):
    try:
        from main import return_pg_connection
        return_pg_connection(c, error=error)
    except Exception:
        try: c.close()
        except Exception: pass


def _split_name(name: str):
    n = (name or "").strip()
    if not n or "@" in n:          # skip empties / emails-as-name
        return ("", "")
    parts = n.split()
    if len(parts) == 1:
        return (parts[0], "")
    return (parts[0], " ".join(parts[1:]))


def _gather():
    """Return (rows, meta). rows = list of dicts first_name/last_name/email/...."""
    c = _conn()
    if c is None:
        return [], {"error": "no_db"}
    suppressed = set()
    users = []
    try:
        with c.cursor() as cur:
            # Suppression set (best-effort — table/column may vary).
            try:
                cur.execute("SELECT lower(email) FROM email_suppression WHERE email IS NOT NULL")
                suppressed = {r[0] for r in cur.fetchall() if r and r[0]}
            except Exception:
                try: c.rollback()
                except Exception: pass
            # Free users with a real email, newest signup first for dedup.
            cur.execute(
                """
                SELECT email, name, company, plan, created_at
                  FROM users
                 WHERE email IS NOT NULL AND email <> '' AND position('@' in email) > 1
                   AND COALESCE(lower(plan), '') <> ALL(%s)
                 ORDER BY created_at DESC NULLS LAST
                """, (list(_PAID),))
            for email, name, company, plan, created in cur.fetchall():
                users.append((email, name, company, plan, created))
    except Exception as e:
        _return(c, error=True)
        return [], {"error": f"{type(e).__name__}: {str(e)[:160]}"}
    _return(c)

    rows, seen, with_name, suppressed_hits = [], set(), 0, 0
    for email, name, company, tier, created in users:
        key = email.strip().lower()
        if not key or key in seen:
            continue
        if key in suppressed:
            suppressed_hits += 1
            continue
        seen.add(key)
        fn, ln = _split_name(name)
        if fn:
            with_name += 1
        rows.append({
            "first_name": fn, "last_name": ln, "email": email.strip(),
            "company": company or "", "tier": (tier or "free"),
            "signed_up": (created.date().isoformat() if created else ""),
        })
    meta = {"total": len(rows), "with_name": with_name,
            "suppressed_excluded": suppressed_hits}
    return rows, meta


@audience_export_bp.route("/api/v1/admin/audience/free-users.csv", methods=["GET"])
def free_users_csv():
    if not _admin_ok():
        return Response("unauthorized\n", status=401, mimetype="text/plain")
    rows, meta = _gather()
    if meta.get("error"):
        return Response(f"error: {meta['error']}\n", status=503, mimetype="text/plain")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["first_name", "last_name", "email", "company", "tier", "signed_up"])
    for r in rows:
        w.writerow([r["first_name"], r["last_name"], r["email"],
                    r["company"], r["tier"], r["signed_up"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             "attachment; filename=dchub_free_users.csv",
                             "X-Total-Rows": str(meta["total"]),
                             "X-With-Name": str(meta["with_name"])})


@audience_export_bp.route("/api/v1/admin/audience/free-users", methods=["GET"])
def free_users_json():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    rows, meta = _gather()
    if meta.get("error"):
        return jsonify(ok=False, **meta), 503
    return jsonify(ok=True, **meta, sample=rows[:10]), 200
