"""
Phase FF+25-followup-enrich (2026-05-20) — signup enrichment stub.
==========================================================================

When a user signs up with a corporate email, look up the company +
industry + employee count + LinkedIn URL via Clearbit (or abstractapi
as cheaper fallback). Persist to user_enrichment table so we can:

  1. Sell ads by audience segment ("80% of users at companies with
     500+ employees in cloud / data center industries")
  2. Personalize MCP responses ("Hi Equinix team, here's the data
     filtered to your portfolio")
  3. Surface user-company graph for trust signals on the homepage
     ("Used by teams at Equinix, Brookfield, AWS Infra…")

Endpoints:
  POST /api/v1/admin/enrich/run            Enrich all existing users
  POST /api/v1/admin/enrich/email          Enrich one specific email
  GET  /api/v1/admin/enrich/status         Token status + cohort stats

Env vars (set whichever you have):
  CLEARBIT_API_KEY       — best quality, $99/mo paid
  ABSTRACT_API_KEY       — cheaper, free tier 100 reqs/month
  HUNTER_API_KEY         — free tier 50 reqs/month, just company name

If NO key is set, this module loads silently and the endpoints return
503 with a clear "set one of these env vars" hint. No data is collected
until you opt in.
"""
import os
import json
import logging
from datetime import datetime, timezone
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
signup_enrichment_bp = Blueprint("signup_enrichment", __name__)


# ── Auth ────────────────────────────────────────────────────────────
_INTERNAL_KEYS = {"dchub-internal-sync-2026"}
for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
    _v = os.environ.get(_n)
    if _v:
        _INTERNAL_KEYS.add(_v)


def _admin_ok():
    sent = (request.headers.get("X-Internal-Key")
            or request.args.get("admin_key") or "").strip()
    return sent in _INTERNAL_KEYS


# ── Provider selection ──────────────────────────────────────────────
def _active_provider():
    """Return (name, key) of the first available enrichment provider."""
    for name, env_var in (("clearbit", "CLEARBIT_API_KEY"),
                           ("abstractapi", "ABSTRACT_API_KEY"),
                           ("hunter", "HUNTER_API_KEY")):
        v = (os.environ.get(env_var) or "").strip()
        if v:
            return name, v
    return None, None


# ── Schema ──────────────────────────────────────────────────────────
def _ensure_table():
    """Create user_enrichment table if it doesn't exist."""
    try:
        from main import get_db
        conn = get_db()
        if conn is None:
            return False
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS user_enrichment (
                        email           TEXT PRIMARY KEY,
                        domain          TEXT,
                        company         TEXT,
                        industry        TEXT,
                        employee_count  INTEGER,
                        country         TEXT,
                        linkedin_url    TEXT,
                        provider        TEXT,
                        raw             TEXT,
                        enriched_at     TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_user_enrich_domain "
                    "ON user_enrichment(domain)"
                )
                conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"[enrich] table create failed: {e}")
        return False


# ── Provider implementations ────────────────────────────────────────
def _enrich_clearbit(email, key):
    """Clearbit Person + Company enrichment. Best quality."""
    import requests
    r = requests.get(
        f"https://person.clearbit.com/v2/combined/find?email={email}",
        headers={"Authorization": f"Bearer {key}"},
        timeout=10,
    )
    if r.status_code != 200:
        return None
    data = r.json() or {}
    company = (data.get("company") or {})
    metrics = (company.get("metrics") or {})
    return {
        "company": company.get("name"),
        "industry": (company.get("category") or {}).get("industry"),
        "employee_count": metrics.get("employees"),
        "country": (company.get("geo") or {}).get("country"),
        "linkedin_url": (company.get("linkedin") or {}).get("handle"),
        "raw": data,
    }


def _enrich_abstract(email, key):
    """abstractapi.com — cheaper, company-only lookup."""
    import requests
    domain = email.split("@")[-1] if "@" in email else email
    r = requests.get(
        "https://companyenrichment.abstractapi.com/v1/",
        params={"api_key": key, "domain": domain},
        timeout=10,
    )
    if r.status_code != 200:
        return None
    data = r.json() or {}
    return {
        "company": data.get("name"),
        "industry": data.get("industry"),
        "employee_count": data.get("employees_count"),
        "country": data.get("country"),
        "linkedin_url": data.get("linkedin_url"),
        "raw": data,
    }


def _enrich_hunter(email, key):
    """hunter.io — company name only, free tier."""
    import requests
    domain = email.split("@")[-1] if "@" in email else email
    r = requests.get(
        "https://api.hunter.io/v2/domain-search",
        params={"domain": domain, "api_key": key, "limit": 1},
        timeout=10,
    )
    if r.status_code != 200:
        return None
    data = (r.json() or {}).get("data") or {}
    return {
        "company": data.get("organization"),
        "industry": data.get("industry"),
        "employee_count": None,
        "country": data.get("country"),
        "linkedin_url": data.get("linkedin"),
        "raw": data,
    }


def _enrich_one(email):
    """Enrich a single email using the active provider."""
    name, key = _active_provider()
    if not key:
        return False, "no_provider_configured"
    if "@" not in email:
        return False, "invalid_email"
    try:
        if name == "clearbit":
            result = _enrich_clearbit(email, key)
        elif name == "abstractapi":
            result = _enrich_abstract(email, key)
        elif name == "hunter":
            result = _enrich_hunter(email, key)
        else:
            return False, f"unknown_provider: {name}"
        if not result:
            return False, "provider_returned_empty"
    except Exception as e:
        return False, f"provider_error: {str(e)[:120]}"

    # Persist
    try:
        from main import get_db
        conn = get_db()
        if conn is None:
            return False, "no_db"
        try:
            with conn.cursor() as cur:
                domain = email.split("@")[-1]
                cur.execute("""
                    INSERT INTO user_enrichment
                        (email, domain, company, industry, employee_count,
                         country, linkedin_url, provider, raw, enriched_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW() ON CONFLICT DO NOTHING)
                    ON CONFLICT (email) DO UPDATE SET
                        company         = EXCLUDED.company,
                        industry        = EXCLUDED.industry,
                        employee_count  = EXCLUDED.employee_count,
                        country         = EXCLUDED.country,
                        linkedin_url    = EXCLUDED.linkedin_url,
                        provider        = EXCLUDED.provider,
                        raw             = EXCLUDED.raw,
                        enriched_at     = NOW()
                """, (email, domain, result.get("company"),
                       result.get("industry"), result.get("employee_count"),
                       result.get("country"), result.get("linkedin_url"),
                       name, json.dumps(result.get("raw") or {})))
                conn.commit()
            return True, "ok"
        finally:
            conn.close()
    except Exception as e:
        return False, f"persist_error: {str(e)[:120]}"


# ── Endpoints ───────────────────────────────────────────────────────
@signup_enrichment_bp.route("/api/v1/admin/enrich/email", methods=["POST"])
def enrich_email():
    """Enrich one specific email. Idempotent."""
    if not _admin_ok():
        return jsonify(error="forbidden"), 403
    if not _active_provider()[1]:
        return jsonify(
            ok=False,
            error="no_enrichment_provider_configured",
            hint=("Set one of: CLEARBIT_API_KEY (paid, best quality), "
                   "ABSTRACT_API_KEY (cheap), or HUNTER_API_KEY (free tier).")
        ), 503
    _ensure_table()
    payload = request.get_json(silent=True) or {}
    email = (payload.get("email") or request.args.get("email") or "").strip().lower()
    if not email:
        return jsonify(ok=False, error="email_required"), 400
    ok, info = _enrich_one(email)
    return jsonify(ok=ok, email=email, info=info,
                    provider=_active_provider()[0])


@signup_enrichment_bp.route("/api/v1/admin/enrich/run", methods=["POST"])
def enrich_run():
    """Enrich every user in api_keys that doesn't have a row in
    user_enrichment yet. Caps at 50/run to stay under free-tier limits."""
    if not _admin_ok():
        return jsonify(error="forbidden"), 403
    if not _active_provider()[1]:
        return jsonify(
            ok=False,
            error="no_enrichment_provider_configured",
            hint="Set CLEARBIT_API_KEY, ABSTRACT_API_KEY, or HUNTER_API_KEY"
        ), 503
    _ensure_table()
    limit = min(int(request.args.get("limit") or 50), 200)
    try:
        from main import get_db
        conn = get_db()
        if conn is None:
            return jsonify(ok=False, error="no_db"), 503
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT DISTINCT email FROM api_keys
                    WHERE email IS NOT NULL AND email != ''
                      AND email NOT IN (SELECT email FROM user_enrichment)
                    LIMIT %s
                """, (limit,))
                emails = [r[0] for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500

    results = []
    for email in emails:
        ok, info = _enrich_one(email)
        results.append({"email": email, "ok": ok, "info": info})
    return jsonify(
        ok=True,
        provider=_active_provider()[0],
        attempted=len(results),
        succeeded=sum(1 for r in results if r["ok"]),
        results=results[:20],
    )


@signup_enrichment_bp.route("/api/v1/admin/enrich/status", methods=["GET"])
def enrich_status():
    """Provider configured? + cohort breakdown of existing enrichment."""
    provider, _ = _active_provider()
    out = {
        "configured": bool(provider),
        "provider": provider,
        "providers_supported": ["clearbit", "abstractapi", "hunter"],
    }
    if provider:
        out["env_vars"] = {
            "CLEARBIT_API_KEY": bool(os.environ.get("CLEARBIT_API_KEY")),
            "ABSTRACT_API_KEY": bool(os.environ.get("ABSTRACT_API_KEY")),
            "HUNTER_API_KEY": bool(os.environ.get("HUNTER_API_KEY")),
        }
        try:
            from main import get_db
            conn = get_db()
            if conn:
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT to_regclass('public.user_enrichment')")
                        if (cur.fetchone() or [None])[0]:
                            cur.execute("SELECT COUNT(*) FROM user_enrichment")
                            out["enriched_users"] = int(cur.fetchone()[0] or 0)
                            cur.execute("""
                                SELECT industry, COUNT(*) AS n
                                FROM user_enrichment
                                WHERE industry IS NOT NULL
                                GROUP BY industry ORDER BY n DESC LIMIT 8
                            """)
                            out["top_industries"] = [
                                {"name": r[0], "users": int(r[1])}
                                for r in cur.fetchall()
                            ]
                        else:
                            out["enriched_users"] = 0
                            out["note"] = "user_enrichment table not yet created — run /enrich/run once"
                finally:
                    conn.close()
        except Exception as e:
            out["_db_error"] = str(e)[:120]
    else:
        out["next_step"] = ("Set one of: CLEARBIT_API_KEY (paid, best), "
                              "ABSTRACT_API_KEY (cheap), HUNTER_API_KEY (free) "
                              "as a Railway env var. Then call "
                              "POST /api/v1/admin/enrich/run to backfill.")
    return jsonify(out)


def _smoke():
    name, _ = _active_provider()
    if name:
        logger.info("[signup-enrichment] ready · provider=%s", name)
    else:
        logger.info("[signup-enrichment] inactive — no provider env var set "
                     "(CLEARBIT_API_KEY / ABSTRACT_API_KEY / HUNTER_API_KEY)")

_smoke()
