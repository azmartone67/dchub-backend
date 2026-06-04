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


# ── Internal helper (callable from same-process code, no admin auth) ─

def _issue_internal(
    partner_slug: str,
    target_email: str,
    plan: str = "developer",
    label: str = "",
    company: str = "",
    name: str = "",
    stripe_url: str = "",
    amount_usd_year=None,
    term_months=None,
    renewal_terms: str = "",
    issued_by: str = "internal",
) -> dict:
    """Mint a partner key WITHOUT admin auth — for in-process callers
    (e.g. ai_lab_outreach pre-mint). Mirrors issue_partner_key()'s DB
    logic exactly so the two paths stay one source of truth.

    Returns a dict {ok, key, key_prefix, partner_slug, email, plan,
    reused (bool), revoked_prior_keys_count, error?}. NEVER raises;
    fail-soft so a caller can degrade gracefully if DB is down.

    Idempotent on (partner_slug, email) — revokes any prior partner key
    for the same pair and returns a fresh one.
    """
    _ensure_partner_table()

    partner_slug = (partner_slug or "").strip().lower()
    email        = (target_email or "").strip().lower()
    plan         = (plan or "developer").strip().lower()
    label        = (label or f"{partner_slug} partner key").strip()
    company      = (company or "").strip()
    name         = (name or "").strip()
    try:
        amount_usd_year = int(amount_usd_year) if amount_usd_year else None
    except Exception:
        amount_usd_year = None
    try:
        term_months = int(term_months) if term_months else None
    except Exception:
        term_months = None

    if not partner_slug or not email:
        return {
            "ok":    False,
            "error": "missing_required_fields",
            "required": ["partner_slug", "email"],
        }
    if plan not in ("free", "developer", "starter", "pro", "enterprise"):
        return {
            "ok":    False,
            "error": "invalid_plan",
            "valid_plans": ["free", "developer", "starter", "pro", "enterprise"],
        }

    # Mint key body
    key_body = secrets.token_urlsafe(32).replace("-", "").replace("_", "")[:32]
    key_str = f"dchub_{plan}_{key_body}"
    key_prefix = f"dchub_{plan}_{key_body[:8]}"
    key_hash = key_str

    c = _db_conn()
    if not c:
        return {"ok": False, "error": "db_unavailable"}

    revoked_count = 0
    try:
        with c.cursor() as cur:
            # Find or create user
            user_id = None
            cur.execute("SELECT id FROM users WHERE email = %s", (email,))
            r = cur.fetchone()
            if r:
                user_id = r[0]
                cur.execute("""
                    UPDATE users SET plan = %s, role = %s, name = COALESCE(NULLIF(%s,''), name),
                                       company = COALESCE(NULLIF(%s,''), company)
                     WHERE id = %s
                """, (plan, plan, name, company, user_id))
            else:
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

            cur.execute("""
                UPDATE partner_keys_issued
                   SET revoked_at = NOW()
                 WHERE partner_slug = %s AND user_email = %s
                   AND revoked_at IS NULL
            """, (partner_slug, email))
            revoked_count = cur.rowcount
            if revoked_count > 0:
                cur.execute("""
                    UPDATE api_keys SET is_active = 0
                     WHERE user_id = %s AND is_active = 1
                """, (user_id,))

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

            cur.execute("""
                INSERT INTO partner_keys_issued
                    (partner_slug, key_prefix, user_email, plan, label,
                     stripe_url, amount_usd_year, term_months,
                     renewal_terms, issued_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
            """, (partner_slug, key_prefix, email, plan, label,
                   (stripe_url or "").strip() or None, amount_usd_year, term_months,
                   (renewal_terms or "").strip() or None, issued_by))
            c.commit()
    except Exception as e:
        try: c.close()
        except Exception: pass
        return {"ok": False, "error": str(e)[:300]}
    finally:
        try: c.close()
        except Exception: pass

    return {
        "ok":            True,
        "issued_at":     datetime.datetime.utcnow().isoformat() + "Z",
        "partner_slug":  partner_slug,
        "email":         email,
        "plan":          plan,
        "key":           key_str,
        "key_prefix":    key_prefix,
        "reused":        False,
        "revoked_prior_keys_count": revoked_count,
    }


# ── Endpoints ───────────────────────────────────────────────────────

@partner_key_issuer_bp.route(
    "/api/v1/admin/partner-key/issue", methods=["POST"]
)
def issue_partner_key():
    """Mint a Developer-tier (or higher) API key for a partner.
    Idempotent on (partner_slug, email) — re-issues if same pair seen
    again, returning a fresh key + revoking the prior one.

    Thin delegator to _issue_internal() — kept as the public admin
    surface; _issue_internal is the same-process pre-mint helper used
    by ai_lab_outreach._draft_pitch() so the two paths share one
    source of truth."""
    if not _admin_authorized():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401

    data = request.get_json(silent=True) or {}
    partner_slug = (data.get("partner_slug") or "").strip().lower()
    email        = (data.get("email") or "").strip().lower()
    name         = (data.get("name") or "").strip()
    plan         = (data.get("plan") or "developer").strip().lower()
    label        = (data.get("label") or f"{partner_slug} partner key").strip()
    company      = (data.get("company") or "").strip()
    stripe_url      = (data.get("stripe_url") or "").strip()
    amount_usd_year = data.get("amount_usd_year")
    term_months    = data.get("term_months")
    renewal_terms = (data.get("renewal_terms") or "").strip()

    result = _issue_internal(
        partner_slug=partner_slug,
        target_email=email,
        plan=plan,
        label=label,
        company=company,
        name=name,
        stripe_url=stripe_url,
        amount_usd_year=amount_usd_year,
        term_months=term_months,
        renewal_terms=renewal_terms,
        issued_by="admin-curl",
    )

    if not result.get("ok"):
        # Map known internal errors to the historical HTTP codes.
        err = result.get("error", "")
        if err == "missing_required_fields":
            return jsonify(result), 400
        if err == "invalid_plan":
            return jsonify(result), 400
        if err == "db_unavailable":
            return jsonify(result), 200
        return jsonify(result), 500

    key_str    = result["key"]
    return jsonify({
        "ok":            True,
        "issued_at":     result["issued_at"],
        "partner_slug":  result["partner_slug"],
        "email":         result["email"],
        "plan":          result["plan"],
        "key":           key_str,
        "key_prefix":    result["key_prefix"],
        "revoked_prior_keys_count": result.get("revoked_prior_keys_count", 0),
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


@partner_key_issuer_bp.route(
    "/api/v1/admin/partner-usage/<slug>", methods=["GET"]
)
def partner_usage(slug):
    """Comprehensive usage analytics for a partner_slug — meeting prep tool.

    Use case: before a check-in with a partner (e.g. NLR JSC kickoff),
    answer "exactly what have they done with their keys?" in one query.

    Returns per-key + aggregate:
      - calls today / this week / this month / all-time
      - day-by-day histogram (default last 30 days, ?days=N to override)
      - first call date + most recent call timestamp
      - active days in window
      - revoked-key history for context

    Joins partner_keys_issued (partner_slug → key_prefix) to api_usage_meter
    (api_key LIKE prefix||'%'). The meter tracks per-day call counts but
    NOT per-endpoint breakdown — for endpoint-level detail, either (a)
    ask the partner directly, or (b) inspect Railway request logs.

    Query params:
      days (int, default 30) — histogram window for daily_calls

    Auth: X-Admin-Key (same as /partner-key/audit).
    """
    if not _admin_authorized():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401
    try:
        days = max(1, min(int(request.args.get("days", 30)), 365))
    except (TypeError, ValueError):
        days = 30

    c = _db_conn()
    if not c:
        return jsonify({"ok": False, "error": "db_unavailable"}), 200
    try:
        with c.cursor() as cur:
            # 1. All keys ever issued for this partner_slug
            cur.execute("""
                SELECT key_prefix, user_email, plan, label,
                       issued_at, revoked_at,
                       stripe_url, amount_usd_year, term_months
                  FROM partner_keys_issued
                 WHERE partner_slug = %s
                 ORDER BY issued_at DESC
            """, (slug,))
            cols = [d[0] for d in cur.description]
            partner_keys = [dict(zip(cols, row)) for row in cur.fetchall()]

            if not partner_keys:
                return jsonify({
                    "ok":           True,
                    "partner_slug": slug,
                    "summary":      {"active_keys": 0, "revoked_keys": 0,
                                     "calls_today": 0, "calls_week": 0,
                                     "calls_month": 0, "calls_total": 0,
                                     "most_recent_call": None},
                    "keys":         [],
                    "note":         "No keys ever issued for this partner_slug.",
                }), 200

            # 2. Per-key usage rollup (api_key LIKE prefix||'%')
            for k in partner_keys:
                k["status"] = "revoked" if k.get("revoked_at") else "active"

                # ISO-format the timestamps that exist
                for fld in ("issued_at", "revoked_at"):
                    if k.get(fld):
                        k[fld] = k[fld].isoformat() if hasattr(k[fld], "isoformat") else str(k[fld])

                if k["status"] == "revoked":
                    # Don't bother rolling up usage on revoked keys
                    continue

                prefix = k["key_prefix"]
                cur.execute("""
                    SELECT
                        COALESCE(SUM(calls_count), 0)::BIGINT                                                  AS calls_total,
                        COALESCE(SUM(CASE WHEN usage_date = CURRENT_DATE                          THEN calls_count ELSE 0 END), 0)::BIGINT  AS calls_today,
                        COALESCE(SUM(CASE WHEN usage_date >= CURRENT_DATE - INTERVAL '7 days'     THEN calls_count ELSE 0 END), 0)::BIGINT  AS calls_week,
                        COALESCE(SUM(CASE WHEN usage_date >= date_trunc('month', CURRENT_DATE)   THEN calls_count ELSE 0 END), 0)::BIGINT  AS calls_month,
                        MAX(last_call_at)                                                                       AS last_call_at,
                        MIN(usage_date)                                                                         AS first_call_date,
                        COUNT(DISTINCT usage_date)::INTEGER                                                     AS active_days
                      FROM api_usage_meter
                     WHERE api_key LIKE %s
                """, (prefix + "%",))
                stat_cols = [d[0] for d in cur.description]
                stats = dict(zip(stat_cols, cur.fetchone() or []))
                # ISO-format the timestamps the meter returns
                for fld in ("last_call_at", "first_call_date"):
                    if stats.get(fld) and hasattr(stats[fld], "isoformat"):
                        stats[fld] = stats[fld].isoformat()
                k.update(stats)

                # Day-by-day histogram
                cur.execute("""
                    SELECT usage_date::TEXT AS d, COALESCE(SUM(calls_count), 0)::BIGINT AS calls
                      FROM api_usage_meter
                     WHERE api_key LIKE %s
                       AND usage_date >= CURRENT_DATE - (%s || ' days')::INTERVAL
                     GROUP BY usage_date
                     ORDER BY usage_date
                """, (prefix + "%", str(days)))
                k["daily_calls"] = [{"date": r[0], "calls": int(r[1] or 0)} for r in cur.fetchall()]

                # r78-f (2026-06-03): per-endpoint breakdown from api_endpoint_log.
                # The log table is created by api_usage_tracker.py at install
                # time; if it doesn't exist yet (e.g. backend pre-r78-c), we
                # swallow the error and return an empty list rather than
                # failing the whole partner-usage call.
                try:
                    cur.execute("""
                        SELECT endpoint_path,
                               COUNT(*)::BIGINT                                   AS calls,
                               MAX(called_at)                                     AS last_call,
                               ROUND(AVG(latency_ms)::numeric, 0)::INTEGER        AS avg_latency_ms,
                               COUNT(*) FILTER (WHERE status >= 200 AND status < 300)::BIGINT AS ok_count,
                               COUNT(*) FILTER (WHERE status >= 400)::BIGINT              AS err_count
                          FROM api_endpoint_log
                         WHERE api_key_prefix = %s
                           AND called_at >= NOW() - (%s || ' days')::INTERVAL
                         GROUP BY endpoint_path
                         ORDER BY calls DESC, endpoint_path
                    """, (prefix, str(days)))
                    rows = cur.fetchall()
                    k["by_endpoint"] = [
                        {
                            "endpoint":       r[0],
                            "calls":          int(r[1] or 0),
                            "last_call":      r[2].isoformat() if r[2] else None,
                            "avg_latency_ms": int(r[3]) if r[3] is not None else None,
                            "ok_count":       int(r[4] or 0),
                            "err_count":      int(r[5] or 0),
                        } for r in rows
                    ]
                except Exception as _ep_err:
                    k["by_endpoint"] = []
                    k["by_endpoint_unavailable"] = str(_ep_err)[:120]

            # 3. Aggregate across ACTIVE keys
            active = [k for k in partner_keys if k["status"] == "active"]
            active_recent = [k.get("last_call_at") for k in active if k.get("last_call_at")]
            agg = {
                "active_keys":      len(active),
                "revoked_keys":     len(partner_keys) - len(active),
                "calls_today":      sum(int(k.get("calls_today", 0))  for k in active),
                "calls_week":       sum(int(k.get("calls_week", 0))   for k in active),
                "calls_month":      sum(int(k.get("calls_month", 0))  for k in active),
                "calls_total":      sum(int(k.get("calls_total", 0))  for k in active),
                "most_recent_call": max(active_recent) if active_recent else None,
                "earliest_call":    min(
                    (k.get("first_call_date") for k in active if k.get("first_call_date")),
                    default=None
                ),
                "days_with_activity": sum(int(k.get("active_days", 0)) for k in active),
            }
    except Exception as e:
        try: c.close()
        except Exception: pass
        return jsonify({"ok": False, "error": str(e)[:300]}), 200
    finally:
        try: c.close()
        except Exception: pass

    return jsonify({
        "ok":           True,
        "partner_slug": slug,
        "as_of":        datetime.datetime.utcnow().isoformat() + "Z",
        "days_window":  days,
        "summary":      agg,
        "keys":         partner_keys,
        "note":         ("Each active-key entry includes a `by_endpoint` array "
                          "(per-endpoint counts + avg latency + ok/err split) "
                          "once api_endpoint_log has data (r78-c+). Daily "
                          "rollup is in `daily_calls`. Per-day per-key "
                          "totals come from api_usage_meter."),
    }), 200


# === Self-service: /api/v1/me/usage =====================================
#
# r78-f (2026-06-03): NLR JSC asked "exactly what have we used?" — and
# they should be able to answer that themselves without needing admin
# access. This endpoint takes any dchub_* API key (their own) and returns
# their usage: per-day, per-endpoint, latency stats, error rates.
#
# Auth: X-API-Key header (caller's own key) — NOT admin auth.
# Public-facing — for partners to inspect their own usage.

@partner_key_issuer_bp.route("/api/v1/me/usage", methods=["GET"])
def me_usage():
    """Self-service usage telemetry for the caller's own API key.

    Pass your key via the standard X-API-Key header (the same one you
    use for any other dchub.cloud API call). Returns:

      - total calls today / week / month / all-time
      - first call date + most recent call timestamp
      - day-by-day histogram (default last 30 days, ?days=N)
      - per-endpoint breakdown (call counts + avg latency + ok/err)

    Notes:
      - Tracking started 2026-06-03 (r78-c). Calls made before that
        date are NOT reflected here.
      - This endpoint is meant to be called interactively or from
        partner integration scripts. It does NOT itself count toward
        your usage stats (the tracker excludes admin/usage paths).

    Example:
        curl -H "X-API-Key: dchub_developer_..." \\
          "https://dchub.cloud/api/v1/me/usage?days=14"
    """
    api_key = (request.headers.get("X-API-Key") or "").strip()
    if not api_key or not api_key.startswith("dchub_") or len(api_key) < 24:
        return jsonify({
            "ok": False,
            "error": "valid_dchub_api_key_required",
            "hint":  "Pass your key via the X-API-Key header. Issue or "
                     "rotate at partnerships@dchub.cloud."
        }), 401

    prefix = api_key[:24]  # match what the tracker stores
    try:
        days = max(1, min(int(request.args.get("days", 30)), 365))
    except (TypeError, ValueError):
        days = 30

    c = _db_conn()
    if not c:
        return jsonify({"ok": False, "error": "db_unavailable"}), 200

    try:
        with c.cursor() as cur:
            # 1. Verify the key actually exists + is active (don't reveal
            #    plan/tier publicly — that's a separate concern; just
            #    confirm the prefix matches a non-revoked key).
            cur.execute("""
                SELECT 1 FROM api_keys
                 WHERE key_prefix = %s AND is_active = 1
                 LIMIT 1
            """, (prefix,))
            if not cur.fetchone():
                return jsonify({
                    "ok": False,
                    "error": "key_not_found_or_inactive",
                    "hint":  "If you believe this is in error, contact "
                             "partnerships@dchub.cloud with this prefix: "
                             f"{prefix}"
                }), 404

            # 2. Aggregate stats from api_usage_meter
            cur.execute("""
                SELECT
                    COALESCE(SUM(calls_count), 0)::BIGINT  AS calls_total,
                    COALESCE(SUM(CASE WHEN usage_date = CURRENT_DATE                          THEN calls_count ELSE 0 END), 0)::BIGINT  AS calls_today,
                    COALESCE(SUM(CASE WHEN usage_date >= CURRENT_DATE - INTERVAL '7 days'     THEN calls_count ELSE 0 END), 0)::BIGINT  AS calls_week,
                    COALESCE(SUM(CASE WHEN usage_date >= date_trunc('month', CURRENT_DATE)   THEN calls_count ELSE 0 END), 0)::BIGINT  AS calls_month,
                    MAX(last_call_at)                       AS last_call_at,
                    MIN(usage_date)                         AS first_call_date,
                    COUNT(DISTINCT usage_date)::INTEGER     AS active_days
                  FROM api_usage_meter
                 WHERE api_key LIKE %s
            """, (prefix + "%",))
            stat_cols = [d[0] for d in cur.description]
            summary = dict(zip(stat_cols, cur.fetchone() or []))
            for fld in ("last_call_at", "first_call_date"):
                if summary.get(fld) and hasattr(summary[fld], "isoformat"):
                    summary[fld] = summary[fld].isoformat()

            # 3. Day-by-day histogram
            cur.execute("""
                SELECT usage_date::TEXT AS d, COALESCE(SUM(calls_count), 0)::BIGINT AS calls
                  FROM api_usage_meter
                 WHERE api_key LIKE %s
                   AND usage_date >= CURRENT_DATE - (%s || ' days')::INTERVAL
                 GROUP BY usage_date
                 ORDER BY usage_date
            """, (prefix + "%", str(days)))
            daily = [{"date": r[0], "calls": int(r[1] or 0)} for r in cur.fetchall()]

            # 4. Per-endpoint breakdown from api_endpoint_log (r78-c+)
            try:
                cur.execute("""
                    SELECT endpoint_path,
                           COUNT(*)::BIGINT                                   AS calls,
                           MAX(called_at)                                     AS last_call,
                           ROUND(AVG(latency_ms)::numeric, 0)::INTEGER        AS avg_latency_ms,
                           COUNT(*) FILTER (WHERE status >= 200 AND status < 300)::BIGINT AS ok_count,
                           COUNT(*) FILTER (WHERE status >= 400)::BIGINT              AS err_count
                      FROM api_endpoint_log
                     WHERE api_key_prefix = %s
                       AND called_at >= NOW() - (%s || ' days')::INTERVAL
                     GROUP BY endpoint_path
                     ORDER BY calls DESC, endpoint_path
                """, (prefix, str(days)))
                rows = cur.fetchall()
                by_endpoint = [
                    {
                        "endpoint":       r[0],
                        "calls":          int(r[1] or 0),
                        "last_call":      r[2].isoformat() if r[2] else None,
                        "avg_latency_ms": int(r[3]) if r[3] is not None else None,
                        "ok_count":       int(r[4] or 0),
                        "err_count":      int(r[5] or 0),
                    } for r in rows
                ]
                by_endpoint_note = None
            except Exception as _ep_err:
                by_endpoint = []
                by_endpoint_note = "endpoint-level tracking not yet available"
    except Exception as e:
        try: c.close()
        except Exception: pass
        return jsonify({"ok": False, "error": str(e)[:200]}), 200
    finally:
        try: c.close()
        except Exception: pass

    return jsonify({
        "ok":           True,
        "key_prefix":   prefix,
        "as_of":        datetime.datetime.utcnow().isoformat() + "Z",
        "days_window":  days,
        "summary":      summary,
        "daily_calls":  daily,
        "by_endpoint":  by_endpoint,
        "by_endpoint_note":  by_endpoint_note,
        "tracking_since":    "2026-06-03 (r78-c). Calls before this date are not reflected.",
    }), 200
