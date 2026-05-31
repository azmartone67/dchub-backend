"""
partner_key_issuer.py — Phase r72 (2026-05-26).

Admin-keyed endpoint to MINT a partner API key on demand.

User landed reVeal/NLR (proposal in DCHub_reVeal_Partnership.pdf,
April 2026) — partnership terms include a "Free Developer API key
provided to NLR team immediately" as step 1 of 5. Gabriel (their
contact) needs the key to integrate the reVeal Characterize module
with DC Hub live endpoints.

This endpoint:
  POST /api/v1/admin/partner-key/issue
  Body: {
    "partner_slug":  "reveal-nlr",            # required, kebab-case
    "email":         "gabriel@reveal...",     # required, key owner
    "name":          "Gabriel <last name>",   # optional, for users.name
    "plan":          "developer",             # default; or pro/enterprise
    "label":         "reVeal Characterize integration",  # api_keys.name
    "company":       "reVeal (NLR)",          # optional, users.company
  }
  → Returns the new key string ONCE (never retrievable again — store it!)

Mirrors the existing INSERT INTO api_keys pattern (main.py:10656)
including the ON CONFLICT clause so re-issuing is idempotent.
Creates the user row if not present.

Sibling:
  GET /api/v1/admin/partner-key/audit — list all partner keys issued
  POST /api/v1/admin/partner-key/revoke/<key_prefix> — kill switch
"""
from __future__ import annotations

import datetime
import hashlib
import os
import secrets

from flask import Blueprint, jsonify, request


partner_key_issuer_bp = Blueprint("partner_key_issuer", __name__)


def _admin_authorized() -> bool:
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "")
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("DCHUB_INTERNAL_KEY") or "")
    return bool(expected) and provided == expected


def _db_conn():
    try:
        import psycopg2
        url = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL"))
        return psycopg2.connect(url, connect_timeout=5) if url else None
    except Exception:
        return None


def _ensure_partner_table():
    """Auxiliary table tracking which keys were issued as partner keys
    (so /audit can filter). Doesn't replace api_keys — supplements it.

    r75 (2026-05-26): added stripe_url + amount_usd_year + term_months
    so renewal conversations can pull the original deal terms inline."""
    c = _db_conn()
    if not c: return
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS partner_keys_issued (
                    id           SERIAL PRIMARY KEY,
                    partner_slug TEXT NOT NULL,
                    key_prefix   TEXT NOT NULL,
                    user_email   TEXT NOT NULL,
                    plan         TEXT NOT NULL,
                    label        TEXT,
                    issued_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    revoked_at   TIMESTAMPTZ,
                    issued_by    TEXT,
                    UNIQUE (key_prefix)
                )
            """)
            # r75 columns — added idempotently after the create
            for col_sql in (
                "ALTER TABLE partner_keys_issued ADD COLUMN IF NOT EXISTS stripe_url TEXT",
                "ALTER TABLE partner_keys_issued ADD COLUMN IF NOT EXISTS amount_usd_year INTEGER",
                "ALTER TABLE partner_keys_issued ADD COLUMN IF NOT EXISTS term_months INTEGER",
                "ALTER TABLE partner_keys_issued ADD COLUMN IF NOT EXISTS renewal_terms TEXT",
            ):
                try: cur.execute(col_sql)
                except Exception: pass
            cur.execute("""
                CREATE INDEX IF NOT EXISTS partner_keys_issued_slug_idx
                    ON partner_keys_issued (partner_slug, issued_at DESC)
            """)
            c.commit()
    except Exception:
        pass
    finally:
        try: c.close()
        except Exception: pass


# ── Endpoints ───────────────────────────────────────────────────────

@partner_key_issuer_bp.route(
    "/api/v1/admin/partner-key/issue", methods=["POST"]
)
def issue_partner_key():
    """Mint a Developer-tier (or higher) API key for a partner.
    Idempotent on (partner_slug, email) — re-issues if same pair seen
    again, returning a fresh key + revoking the prior one."""
    if not _admin_authorized():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401
    _ensure_partner_table()

    data = request.get_json(silent=True) or {}
    partner_slug = (data.get("partner_slug") or "").strip().lower()
    email        = (data.get("email") or "").strip().lower()
    name         = (data.get("name") or "").strip()
    plan         = (data.get("plan") or "developer").strip().lower()
    label        = (data.get("label") or f"{partner_slug} partner key").strip()
    company      = (data.get("company") or "").strip()
    # r75: optional deal-terms fields (all best-effort, no validation)
    stripe_url      = (data.get("stripe_url") or "").strip()
    amount_usd_year = data.get("amount_usd_year")
    try:
        amount_usd_year = int(amount_usd_year) if amount_usd_year else None
    except Exception:
        amount_usd_year = None
    term_months    = data.get("term_months")
    try:
        term_months = int(term_months) if term_months else None
    except Exception:
        term_months = None
    renewal_terms = (data.get("renewal_terms") or "").strip()

    if not partner_slug or not email:
        return jsonify({
            "ok":    False,
            "error": "missing_required_fields",
            "required": ["partner_slug", "email"],
        }), 400

    if plan not in ("free", "developer", "starter", "pro", "enterprise"):
        return jsonify({
            "ok":    False,
            "error": "invalid_plan",
            "valid_plans": ["free", "developer", "starter", "pro", "enterprise"],
        }), 400

    # Mint key: prefix carries plan + partner_slug for human readability
    # in dashboards. Body is 32 cryptographic chars.
    key_body = secrets.token_urlsafe(32).replace("-", "").replace("_", "")[:32]
    key_str = f"dchub_{plan}_{key_body}"
    key_prefix = f"dchub_{plan}_{key_body[:8]}"  # First 8 chars shown
    key_hash = key_str   # api_keys.key_hash stores the full string per
                          # the existing schema (see main.py:10656 INSERT)

    c = _db_conn()
    if not c:
        return jsonify({"ok": False, "error": "db_unavailable"}), 200

    try:
        with c.cursor() as cur:
            # Find or create the user
            user_id = None
            cur.execute("SELECT id FROM users WHERE email = %s", (email,))
            r = cur.fetchone()
            if r:
                user_id = r[0]
                # Update plan in case it changed
                cur.execute("""
                    UPDATE users SET plan = %s, role = %s, name = COALESCE(NULLIF(%s,''), name),
                                       company = COALESCE(NULLIF(%s,''), company)
                     WHERE id = %s
                """, (plan, plan, name, company, user_id))
            else:
                # Create new user with random ID + a placeholder password
                # (partner can set password via /forgot-password later)
                user_id = secrets.token_hex(16)
                placeholder_pw = hashlib.sha256(
                    secrets.token_urlsafe(32).encode()
                ).hexdigest()
                cur.execute("""
                    INSERT INTO users (id, email, password_hash, name, company,
                                          role, plan, api_calls_today, api_calls_total)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 0, 0) ON CONFLICT DO NOTHING
                """, (user_id, email, placeholder_pw, name or email.split("@")[0],
                       company, plan, plan))

            # Revoke any prior partner key for this (partner_slug, email)
            cur.execute("""
                UPDATE partner_keys_issued
                   SET revoked_at = NOW()
                 WHERE partner_slug = %s AND user_email = %s
                   AND revoked_at IS NULL
            """, (partner_slug, email))
            revoked_count = cur.rowcount
            # Also deactivate the old api_keys rows for this user
            if revoked_count > 0:
                cur.execute("""
                    UPDATE api_keys SET is_active = 0
                     WHERE user_id = %s AND is_active = 1
                """, (user_id,))

            # Mint the new key in api_keys
            rate_limit_tier = {
                "free":       "free",
                "developer":  "developer",
                "starter":    "starter",
                "pro":        "pro",
                "enterprise": "enterprise",
            }.get(plan, plan)
            cur.execute("""
                INSERT INTO api_keys
                    (user_id, key_hash, key_prefix, name, permissions,
                     rate_limit_tier, is_active, created_at,
                     usage_count, plan, calls_today, calls_total)
                VALUES
                    (%s, %s, %s, %s, '["read","write"]',
                     %s, 1, NOW() ON CONFLICT DO NOTHING,
                     0, %s, 0, 0)
                ON CONFLICT (key_hash) DO UPDATE SET
                    is_active = 1, plan = EXCLUDED.plan
            """, (user_id, key_hash, key_prefix, label,
                   rate_limit_tier, plan))

            # Record in partner audit log
            # r75: persist deal terms (stripe_url, amount_usd_year,
            # term_months, renewal_terms) alongside the key
            cur.execute("""
                INSERT INTO partner_keys_issued
                    (partner_slug, key_prefix, user_email, plan, label,
                     stripe_url, amount_usd_year, term_months,
                     renewal_terms, issued_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
            """, (partner_slug, key_prefix, email, plan, label,
                   stripe_url or None, amount_usd_year, term_months,
                   renewal_terms or None, "admin-curl"))
            c.commit()

    except Exception as e:
        try: c.close()
        except Exception: pass
        return jsonify({"ok": False, "error": str(e)[:300]}), 500
    finally:
        try: c.close()
        except Exception: pass

    return jsonify({
        "ok":            True,
        "issued_at":     datetime.datetime.utcnow().isoformat() + "Z",
        "partner_slug":  partner_slug,
        "email":         email,
        "plan":          plan,
        "key":           key_str,
        "key_prefix":    key_prefix,
        "revoked_prior_keys_count": revoked_count,
        "header_usage":  f"X-API-Key: {key_str}",
        "test_call":     (f"curl -H 'X-API-Key: {key_str}' "
                            "'https://dchub.cloud/api/v1/site-forecast"
                            "?lat=39.04&lon=-77.48&state=VA'"),
        "warning":       ("Store this key NOW — the full string is not "
                            "retrievable after this response. Only the "
                            "key_prefix is shown in /audit."),
    }), 200


@partner_key_issuer_bp.route(
    "/api/v1/admin/partner-key/audit", methods=["GET"]
)
def audit_partner_keys():
    """List all partner keys issued (active + revoked)."""
    if not _admin_authorized():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401
    _ensure_partner_table()

    c = _db_conn()
    if not c:
        return jsonify({"ok": False, "error": "db_unavailable"}), 200
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, partner_slug, key_prefix, user_email, plan,
                       label, issued_at, revoked_at, issued_by,
                       stripe_url, amount_usd_year, term_months,
                       renewal_terms
                  FROM partner_keys_issued
                 ORDER BY issued_at DESC
                 LIMIT 200
            """)
            rows = cur.fetchall() or []
    except Exception as e:
        try: c.close()
        except Exception: pass
        return jsonify({"ok": False, "error": str(e)[:200]}), 200
    finally:
        try: c.close()
        except Exception: pass

    return jsonify({
        "ok":    True,
        "count": len(rows),
        "keys":  [{
            "id":            r[0],
            "partner_slug":  r[1],
            "key_prefix":    r[2],
            "user_email":    r[3],
            "plan":          r[4],
            "label":         r[5],
            "issued_at":     r[6].isoformat() if r[6] else None,
            "revoked_at":    r[7].isoformat() if r[7] else None,
            "is_active":     r[7] is None,
            "issued_by":     r[8],
            "stripe_url":    r[9],
            "amount_usd_year": r[10],
            "term_months":   r[11],
            "renewal_terms": r[12],
        } for r in rows],
    }), 200


@partner_key_issuer_bp.route(
    "/api/v1/admin/partner-key/revoke/<key_prefix>", methods=["POST"]
)
def revoke_partner_key(key_prefix):
    """Kill switch — deactivate a partner key by prefix."""
    if not _admin_authorized():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401
    c = _db_conn()
    if not c:
        return jsonify({"ok": False, "error": "db_unavailable"}), 200
    try:
        with c.cursor() as cur:
            cur.execute("""
                UPDATE partner_keys_issued
                   SET revoked_at = NOW()
                 WHERE key_prefix = %s AND revoked_at IS NULL
             RETURNING partner_slug, user_email
            """, (key_prefix,))
            row = cur.fetchone()
            if row:
                cur.execute("""
                    UPDATE api_keys SET is_active = 0
                     WHERE key_prefix = %s
                """, (key_prefix,))
            c.commit()
    except Exception as e:
        try: c.close()
        except Exception: pass
        return jsonify({"ok": False, "error": str(e)[:200]}), 200
    finally:
        try: c.close()
        except Exception: pass

    if not row:
        return jsonify({"ok": False, "error": "key_not_found_or_already_revoked"}), 404
    return jsonify({
        "ok":           True,
        "key_prefix":   key_prefix,
        "partner_slug": row[0],
        "user_email":   row[1],
        "revoked_at":   datetime.datetime.utcnow().isoformat() + "Z",
    }), 200
