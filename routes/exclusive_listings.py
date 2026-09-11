"""
exclusive_listings.py — DC Hub Pocket Listings: off-market data center capacity,
an auth wall, and a lead register operators can verify.

Phase GG (2026-05-14) created the table and a Pro-only feed that handed Pro
callers the operator's contact details. 2026-09-11 rebuilt the program around
what it actually has to prove: that a prospect reached the operator THROUGH
DC Hub.

  * AUTH WALL, not a paywall, by default. `tier_required` is `registered`
    unless an admin sets `pro` / `enterprise` / `founding` on a listing.
    Teasers (market, state, capacity, status) are public — they are the demand
    driver. Full detail needs an identified END USER: a signed-in web user, an
    agent key with an email bound, or an OAuth-connected agent.
  * Operator contact is never served. A buyer holding the operator's phone
    number has no reason to let DC Hub make the introduction, and leaves no
    trace that DC Hub did. Prospects request an introduction instead, and the
    operator's details stay admin-only.
  * Every request, identified view, confirmation and operator notice is an
    entry in util/listing_ledger.py's hash chain.
    GET /api/v1/listings/leads/<id>/verify shows any party that record.

★ THE TRANSPORT TRAP. dchub-mcp-server's callAPI() sends X-Internal-Key on
every call, and map_tier_gating._detect_caller_tier maps that header to 'pro'
at STEP 1. Resolved naively, every anonymous agent would open every walled
listing while every REST test passed. The key proves provenance ("this came
through our MCP server"), never identity, so _detect_identity hides it for the
duration of the lookup — routes/mcp_tier1_tools._end_user_tier's pattern. The
admin endpoints accept ONLY X-Admin-Key == DCHUB_ADMIN_KEY for the same reason:
the gateway holds the internal key, and lead records carry prospect PII.

Endpoints:
    GET  /api/v1/listings                          teaser feed + program + viewer
    GET  /api/v1/listings/terms                    introduction terms
    GET  /api/v1/listings/health
    GET  /api/v1/listings/<slug_or_id>             detail (walled)
    POST /api/v1/listings/<slug_or_id>/intro       request an introduction
    POST /api/v1/listings/interest                 register a requirement
    POST /api/v1/listings/leads/confirm            prospect confirms from inbox
    GET  /api/v1/listings/leads/<lead_id>/verify   public registration record
    GET  /api/v1/listings/<slug_or_id>/leads       operator ledger (?token=)
  admin (X-Admin-Key):
    GET  /api/v1/admin/listings                    every listing incl. contact
    POST /api/v1/admin/listings                    create
    PUT|PATCH|DELETE /api/v1/admin/listings/<id>
    GET  /api/v1/admin/listings/leads              ledger rows with PII
    POST /api/v1/admin/listings/leads/<lead_id>/notify-operator
    POST /api/v1/admin/listings/leads/<lead_id>/status   introduced | withdrawn
    POST /api/v1/admin/listings/<id>/operator-link
    GET  /api/v1/admin/listings/ledger/verify

Listing `contact` (admin-only JSON): {name, company, email, phone,
notify_email, auto_notify}. With auto_notify true, a confirmed lead also sends
the operator a registration notice; otherwise the admin sends it.
"""
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from flask import Blueprint, jsonify, request

from util import listing_ledger as ledger

logger = logging.getLogger(__name__)

exclusive_listings_bp = Blueprint("exclusive_listings", __name__)

SITE = "https://dchub.cloud"
PRICING_URL = f"{SITE}/pricing"
SUPPORT_EMAIL = "hello@dchub.cloud"

# ── introduction terms ────────────────────────────────────────────────────
# Served from GET /api/v1/listings/terms; the web page and MCP tools render
# them from there, so this is the one place to edit. Bump TERMS_VERSION on any
# change — requests carry the version the prospect accepted.
TERMS_VERSION = "2026-09-11"
TERMS_URL = f"{SITE}/listings#terms"
TERMS_SUMMARY = (
    "DC Hub introduces you to this opportunity and records the request in its "
    "lead register. DC Hub may share your name, company, role and requirement "
    "with the operator. For 12 months you agree to pursue a listed opportunity "
    "through DC Hub rather than approaching its operator directly."
)
TERMS_TEXT = (
    "DC Hub Pocket Listings — introduction terms (version " + TERMS_VERSION + ")\n\n"
    "1. What DC Hub does. DC Hub publishes off-market data center capacity on "
    "behalf of operators and introduces prospects to them. DC Hub is not a "
    "party to any agreement between you and an operator.\n\n"
    "2. Registration. When you request an introduction or register a "
    "requirement, DC Hub records who asked, when, for which listing and through "
    "which channel (the website or an AI agent) in a tamper-evident lead "
    "register, and asks you to confirm the request from your inbox. DC Hub may "
    "give the operator your name, company, role, the domain of your email "
    "address and your stated requirement so the operator can verify the "
    "registration.\n\n"
    "3. Confidentiality and introductions. Listing details are provided only "
    "for evaluating the opportunity. For 12 months from your registration you "
    "agree not to contact the operator about a listed opportunity except "
    "through DC Hub, and to tell DC Hub if the operator contacts you about it "
    "directly.\n\n"
    "4. No warranty. Listing information comes from operators and is provided "
    "as is. Verify it independently before relying on it.\n\n"
    "5. Your data. DC Hub uses your details to make and track the introduction "
    "and to email you about this request, not for marketing without your "
    "separate opt-in. To withdraw a request, email " + SUPPORT_EMAIL + "."
)

PROGRAM_NAME = "DC Hub Pocket Listings"
PROGRAM_HEADLINE = "Off-market data center capacity, introduced by DC Hub"
PROGRAM_SUMMARY = (
    "Powered shells, available MW and development sites that are not publicly "
    "marketed. Browse the teasers, sign in to open a listing, and let DC Hub "
    "introduce you to the operator."
)
PROGRAM_STEPS = (
    "Browse teaser cards — market, state and capacity — without an account.",
    "Sign in, or connect an identified AI agent, to open the full listing.",
    "Request an introduction. DC Hub registers the request and introduces you "
    "to the operator; operator contact details are never published.",
)
UPCOMING_NOTE = (
    "The first listings are being onboarded. Register a requirement to get "
    "first access when they open."
)
HOW_TO_VERIFY = (
    "Each event is an entry in DC Hub's append-only lead register. entry_hash "
    "is the SHA-256 of the entry's canonical record, and every entry includes "
    "the hash of the entry before it, so altering, removing or back-dating any "
    "entry changes every hash after it. DC Hub's registration notice to the "
    "operator quotes these hashes: if the hashes in that email match this "
    "record, it has not changed since the notice was sent, and the email's "
    "receipt time shows when the registration already existed."
)

_ACCESS_LEVELS = ("registered", "pro", "enterprise", "founding")
_REQUIRED_RANK = {"registered": 1, "pro": 3, "founding": 3, "enterprise": 4}
_TIER_RANK = {"anonymous": 0, "": 0, "free": 1, "identified": 1,
              "registered": 1, "starter": 1, "developer": 2, "pro": 3,
              "founding": 3, "team": 3, "paid": 3, "metered": 3,
              "enterprise": 4, "admin": 4}
_VALID_STATUSES = ("draft", "pocket", "public")
# Path words under /api/v1/listings/ that must never become a listing slug.
_RESERVED_SLUGS = frozenset({"interest", "leads", "terms", "health", "admin",
                             "verify", "confirm", "intro"})
_SLUG_RE = re.compile(r"[^a-z0-9-]+")
_TOKEN_CHARS_RE = re.compile(r"[^a-z0-9._/-]+")
_DETAIL_PRIVATE_KEYS = frozenset({"contact", "operator_contact", "operator_email",
                                  "notify_email", "internal", "internal_notes",
                                  "owner", "owner_id"})

_LISTING_COLS = ("id", "slug", "title", "summary", "status", "tier_required",
                 "market", "state", "country", "latitude", "longitude",
                 "capacity_mw", "asking_price", "asking_currency", "detail",
                 "contact", "owner_id", "created_at", "updated_at", "expires_at")
_LEDGER_COLS = ("seq", "lead_id", "event", "listing_id", "listing_slug",
                "listing_title", "user_ref", "email", "name", "role", "company",
                "message", "requirement", "email_domain", "email_verified",
                "verified_via", "channel", "platform", "client", "session_hash",
                "ip_hash", "terms_version", "meta", "created_at", "prev_hash",
                "entry_json", "entry_hash", "signature", "key_id")

_LEAD_RATE_LIMIT = (8, 3600)        # lead registrations per identity per hour
_CONFIRM_RATE_LIMIT = (30, 3600)    # confirmation attempts per IP per hour
_READ_RATE_LIMIT = (240, 3600)      # verify / ledger reads per IP per hour
_DUP_WINDOW = {"intro_requested": timedelta(days=30),
               "interest_registered": timedelta(hours=24)}
_RESEND_AFTER_S = 600
_SEND_WAIT_S = 8.0
_CHAIN_TTL_S = 60
_CHAIN_MAX_ROWS = 20000


# ═════════════════════════════════════════════════════════════════════════
#  database
# ═════════════════════════════════════════════════════════════════════════

def _dsn():
    return os.environ.get("DATABASE_URL") or ""


def _conn():
    import psycopg2
    return psycopg2.connect(_dsn(), connect_timeout=8)


_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS exclusive_listings (
    id              BIGSERIAL PRIMARY KEY,
    slug            TEXT UNIQUE NOT NULL,
    title           TEXT NOT NULL,
    summary         TEXT,
    status          TEXT NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft', 'pocket', 'public')),
    tier_required   TEXT NOT NULL DEFAULT 'registered'
                        CHECK (tier_required IN ('registered', 'pro', 'enterprise', 'founding')),
    market          TEXT,
    state           TEXT,
    country         TEXT DEFAULT 'US',
    latitude        REAL,
    longitude       REAL,
    capacity_mw     REAL,
    asking_price    NUMERIC,
    asking_currency TEXT DEFAULT 'USD',
    detail          JSONB,
    contact         JSONB,
    owner_id        TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_exclusive_listings_status
    ON exclusive_listings (status, updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_exclusive_listings_market
    ON exclusive_listings (market) WHERE status <> 'draft';
"""

# Tables created before 2026-09-11 carry CHECK (tier_required IN ('pro',
# 'enterprise', 'founding')), which rejects the auth-wall level. Widen it only
# when the live definition lacks 'registered' — an unconditional DROP/ADD
# would request ACCESS EXCLUSIVE on every boot (see util/ddl_once.py).
_ACCESS_WALL_DDL = """
DO $$
DECLARE
    r record;
    widened boolean := false;
BEGIN
    FOR r IN
        SELECT conname, pg_get_constraintdef(oid) AS def
          FROM pg_constraint
         WHERE conrelid = 'exclusive_listings'::regclass
           AND contype = 'c'
           AND pg_get_constraintdef(oid) ILIKE '%tier_required%'
    LOOP
        IF r.def NOT ILIKE '%registered%' THEN
            EXECUTE format('ALTER TABLE exclusive_listings DROP CONSTRAINT %I', r.conname);
            widened := true;
        END IF;
    END LOOP;
    IF widened THEN
        ALTER TABLE exclusive_listings
            ADD CONSTRAINT exclusive_listings_tier_required_check
            CHECK (tier_required IN ('registered', 'pro', 'enterprise', 'founding'));
        ALTER TABLE exclusive_listings ALTER COLUMN tier_required SET DEFAULT 'registered';
    END IF;
END
$$
"""

_SCHEMA_KEYS = ("exclusive_listings.table", "exclusive_listings.access_wall_v2",
                "listing_lead_ledger.table", "listing_lead_ledger.guard")


def _ensure_schema():
    """Once per process, per key (util/ddl_once). Never at import time."""
    if not _dsn():
        return
    try:
        from util import ddl_once
    except Exception:
        return
    if all(ddl_once.already_done(k) for k in _SCHEMA_KEYS):
        return
    try:
        c = _conn()
    except Exception as exc:
        logger.warning("[pocket-listings] schema connect failed: %s", exc)
        return
    try:
        lock = "SET LOCAL lock_timeout = '3s'"
        ddl_once.ensure_once(_SCHEMA_KEYS[0], c, (lock, _SCHEMA_DDL))
        ddl_once.ensure_once(_SCHEMA_KEYS[1], c, (lock, _ACCESS_WALL_DDL))
        ddl_once.ensure_once(_SCHEMA_KEYS[2], c, ledger.SCHEMA_STATEMENTS)
        ddl_once.ensure_once(_SCHEMA_KEYS[3], c, ledger.GUARD_STATEMENTS)
    finally:
        _close(c)


def _close(c):
    try:
        c.close()
    except Exception:
        pass


def _fetch(sql, params, cols):
    c = _conn()
    try:
        with c.cursor() as cur:
            cur.execute(sql, params)
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        _close(c)


def _db_list_listings(market=None, state=None, min_mw=None, limit=50):
    where = ["status IN ('public', 'pocket')",
             "(expires_at IS NULL OR expires_at > NOW())"]
    params = []
    if market:
        where.append("LOWER(market) = LOWER(%s)")
        params.append(market)
    if state:
        where.append("UPPER(state) = %s")
        params.append(state)
    if min_mw is not None:
        where.append("capacity_mw >= %s")
        params.append(min_mw)
    sql = (f"SELECT {', '.join(_LISTING_COLS)} FROM exclusive_listings "
           f"WHERE {' AND '.join(where)} ORDER BY updated_at DESC LIMIT %s")
    return _fetch(sql, params + [limit], _LISTING_COLS)


def _db_count_live():
    rows = _fetch("SELECT COUNT(*) FROM exclusive_listings "
                  "WHERE status IN ('public', 'pocket') "
                  "AND (expires_at IS NULL OR expires_at > NOW())", [], ("n",))
    return int(rows[0]["n"] or 0) if rows else 0


def _db_count_matching(requirement):
    where = ["status IN ('public', 'pocket')",
             "(expires_at IS NULL OR expires_at > NOW())"]
    params = []
    markets = [m.lower() for m in requirement.get("markets") or []]
    states = [s.upper() for s in requirement.get("states") or []]
    if markets or states:
        where.append("(LOWER(market) = ANY(%s) OR UPPER(state) = ANY(%s))")
        params += [markets, states]
    if requirement.get("capacity_mw") is not None:
        where.append("(capacity_mw IS NULL OR capacity_mw >= %s)")
        params.append(requirement["capacity_mw"])
    rows = _fetch(f"SELECT COUNT(*) FROM exclusive_listings WHERE {' AND '.join(where)}",
                  params, ("n",))
    return int(rows[0]["n"] or 0) if rows else 0


def _db_get_listing(slug_or_id):
    ident = str(slug_or_id or "").strip()
    if not ident:
        return None
    if ident.isdigit():
        sql, param = "id = %s", int(ident)
    else:
        sql, param = "slug = %s", ident
    rows = _fetch(f"SELECT {', '.join(_LISTING_COLS)} FROM exclusive_listings "
                  f"WHERE {sql}", [param], _LISTING_COLS)
    return rows[0] if rows else None


def _db_all_listings():
    return _fetch(f"SELECT {', '.join(_LISTING_COLS)} FROM exclusive_listings "
                  "ORDER BY updated_at DESC LIMIT 500", [], _LISTING_COLS)


def _db_lead_events(lead_id):
    return _fetch(f"SELECT {', '.join(_LEDGER_COLS)} FROM listing_lead_ledger "
                  "WHERE lead_id = %s ORDER BY seq ASC LIMIT 500",
                  [lead_id], _LEDGER_COLS)


def _db_listing_events(listing_id):
    return _fetch(f"SELECT {', '.join(_LEDGER_COLS)} FROM listing_lead_ledger "
                  "WHERE listing_id = %s ORDER BY seq ASC LIMIT 5000",
                  [listing_id], _LEDGER_COLS)


def _db_user_openings(user_ref, event, listing_id, since):
    if listing_id is None:
        clause, params = "listing_id IS NULL", [user_ref, event, since]
    else:
        clause, params = "listing_id = %s", [user_ref, event, since, listing_id]
    return _fetch(f"SELECT {', '.join(_LEDGER_COLS)} FROM listing_lead_ledger "
                  f"WHERE user_ref = %s AND event = %s AND created_at > %s AND {clause} "
                  "ORDER BY seq ASC LIMIT 50", params, _LEDGER_COLS)


def _db_recent_view(user_ref, listing_id, since):
    rows = _fetch("SELECT seq FROM listing_lead_ledger WHERE user_ref = %s "
                  "AND listing_id = %s AND event = 'listing_viewed' "
                  "AND created_at > %s LIMIT 1", [user_ref, listing_id, since], ("seq",))
    return bool(rows)


def _db_admin_ledger(listing_id=None, lead_id=None, limit=500):
    where, params = ["TRUE"], []
    if listing_id is not None:
        where.append("listing_id = %s")
        params.append(listing_id)
    if lead_id:
        where.append("lead_id = %s")
        params.append(lead_id)
    return _fetch(f"SELECT {', '.join(_LEDGER_COLS)} FROM listing_lead_ledger "
                  f"WHERE {' AND '.join(where)} ORDER BY seq DESC LIMIT %s",
                  params + [limit], _LEDGER_COLS)


def _db_chain_rows(limit):
    return _fetch("SELECT seq, lead_id, event, listing_id, created_at, prev_hash, "
                  "entry_json, entry_hash, signature FROM listing_lead_ledger "
                  "ORDER BY seq ASC LIMIT %s", [limit],
                  ("seq", "lead_id", "event", "listing_id", "created_at",
                   "prev_hash", "entry_json", "entry_hash", "signature"))


def _db_verified_via(viewer):
    """'google' / 'oauth' when the identity's inbox is already proven by a
    third party; None means DC Hub has to prove it with a confirmation link."""
    if viewer.get("identity_source") == "session" and viewer.get("user_id"):
        rows = _fetch("SELECT COALESCE(google_id, '') FROM users WHERE id = %s",
                      [str(viewer["user_id"])], ("google_id",))
        if rows and str(rows[0]["google_id"] or "").strip():
            return "google"
    if viewer.get("identity_source") == "api_key" and viewer.get("api_key"):
        rows = _fetch("SELECT COALESCE(metadata->>'source', '') FROM mcp_dev_keys "
                      "WHERE api_key = %s", [viewer["api_key"]], ("source",))
        if rows and rows[0]["source"] == "workos_oauth":
            return "oauth"
    return None


def _append_event(**kwargs):
    secret = kwargs.pop("secret")
    try:
        c = _conn()
    except Exception as exc:
        raise ledger.LedgerUnavailable(
            f"ledger connect failed: {type(exc).__name__}") from exc
    try:
        rec = ledger.append(c, secret, **kwargs)
    finally:
        _close(c)
    _CHAIN_CACHE["at"] = 0.0
    return rec


_CHAIN_CACHE = {"at": 0.0, "value": None}


def _chain_summary(force=False):
    now = time.time()
    cached = _CHAIN_CACHE.get("value")
    if cached is not None and not force and now - _CHAIN_CACHE["at"] < _CHAIN_TTL_S:
        return cached
    rows = _db_chain_rows(_CHAIN_MAX_ROWS + 1)
    summary = ledger.verify_chain(rows[:_CHAIN_MAX_ROWS], ledger.ledger_keys())
    summary["partial"] = len(rows) > _CHAIN_MAX_ROWS
    summary["checked_at"] = ledger.iso_utc(datetime.now(timezone.utc))
    _CHAIN_CACHE.update(at=now, value=summary)
    return summary


def _safe_chain_summary():
    try:
        return _chain_summary()
    except Exception as exc:
        logger.warning("[pocket-listings] chain summary failed: %s", exc)
        return {"intact": None, "entries_checked": 0, "first_break_seq": None,
                "head_seq": None, "head_hash": None, "partial": False,
                "signatures_checked": None, "checked_at": None,
                "error": "chain_unavailable"}


# ═════════════════════════════════════════════════════════════════════════
#  who is asking
# ═════════════════════════════════════════════════════════════════════════

def _decode_jwt(token):
    try:
        import jwt as _jwt
        secret = os.environ.get("JWT_SECRET") or os.environ.get("SECRET_KEY") or ""
        if not secret:
            return None         # never verify against an empty key
        return _jwt.decode(token, secret, algorithms=["HS256"])
    except Exception:
        return None


def _detect_identity():
    """(tier, info) for the END USER. X-Internal-Key is hidden from the
    resolver: it identifies our MCP gateway, not the person behind the agent."""
    env = request.environ
    stashed = env.pop("HTTP_X_INTERNAL_KEY", None)
    try:
        import map_tier_gating
        return map_tier_gating.detect_tier_for_data_gate(decode_jwt_func=_decode_jwt)
    except Exception as exc:
        logger.debug("[pocket-listings] identity lookup failed: %s", exc)
        return "anonymous", None
    finally:
        if stashed is not None:
            env["HTTP_X_INTERNAL_KEY"] = stashed


def _presented_api_key():
    key = (request.headers.get("X-API-Key") or request.args.get("api_key") or "").strip()
    if not key:
        auth = request.headers.get("Authorization") or ""
        if auth.startswith("Bearer ") and auth[7:].strip().startswith(("dchub_", "dch_")):
            key = auth[7:].strip()
    return key


def _is_gateway():
    raw = request.headers.get("X-Internal-Key") or ""
    if not raw:
        return False
    try:
        from internal_auth import is_valid_internal_key
        return bool(is_valid_internal_key(raw))
    except Exception:
        return False


def _viewer():
    tier, info = _detect_identity()
    tier = str(tier or "anonymous").strip().lower() or "anonymous"
    info = info if isinstance(info, dict) else {}
    source = info.get("source")
    api_key = _presented_api_key()
    email = str(info.get("email") or "").strip().lower() or None

    if tier == "anonymous" or source in ("unverified_credential_denied", "internal",
                                         "credentialed_failopen"):
        identity_source, user_ref, email = None, None, None
    elif source == "refresh_cookie" or "exp" in info:
        identity_source = "session"
        user_ref = str(info.get("user_id") or "") or None
    elif api_key:
        identity_source = "api_key"
        user_ref = (str(info["user_id"]) if info.get("user_id") else
                    "key:" + hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16])
    else:
        identity_source = "session"
        user_ref = str(info.get("user_id") or "") or None

    identified = bool(identity_source and user_ref and email and "@" in email)
    if identified:
        reason = None
    elif identity_source == "api_key":
        reason = "email_binding_required"
    else:
        reason = "sign_in_required"

    if _is_gateway():
        channel = "mcp"
    elif identity_source == "api_key":
        channel = "api"
    else:
        channel = "web"
    platform = _TOKEN_CHARS_RE.sub(
        "", (request.headers.get("X-MCP-Platform") or "").strip().lower())[:40] or None
    session = (request.headers.get("X-MCP-Session")
               or request.headers.get("Mcp-Session-Id") or "").strip()
    return {
        "tier": tier,
        "identified": identified,
        "email": email if identified else None,
        "user_ref": user_ref if identified else None,
        "user_id": info.get("user_id") if identified else None,
        "identity_source": identity_source,
        "channel": channel,
        "platform": platform,
        "session_hash": (hashlib.sha256(session.encode("utf-8")).hexdigest()[:16]
                         if session else None),
        "api_key": api_key or None,
        "reason": reason,
    }


def _viewer_public(v, return_path):
    return {
        "identified": v["identified"],
        "tier": v["tier"],
        "channel": v["channel"],
        "email_masked": ledger.mask_email(v["email"]) if v["identified"] else None,
        "identity_source": v["identity_source"],
        "sign_in_url": _sign_in_url(return_path),
    }


def _ip_hash():
    ip = (request.headers.get("CF-Connecting-IP")
          or (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
          or request.remote_addr or "")
    if not ip:
        return None
    return hashlib.sha256(("dchub-listings|" + ip).encode("utf-8")).hexdigest()[:16]


def _user_agent():
    return (request.headers.get("User-Agent") or "")[:300] or None


def _admin_ok():
    """X-Admin-Key == DCHUB_ADMIN_KEY, read at request time, header only.
    Deliberately NOT internal_auth.require_internal_or_admin: that accepts
    X-Internal-Key, which our MCP gateway attaches to every agent call."""
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    expected = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()
    return bool(provided and expected and hmac.compare_digest(provided, expected))


# ═════════════════════════════════════════════════════════════════════════
#  shaping
# ═════════════════════════════════════════════════════════════════════════

def _err(status, code, message, **extra):
    body = {"ok": False, "error": code, "message": message}
    body.update(extra)
    resp = jsonify(body)
    resp.headers["Cache-Control"] = "private, no-store"
    return resp, status


def _no_store(resp):
    resp.headers["Cache-Control"] = "private, no-store"
    resp.headers["Vary"] = "Authorization, Cookie, X-API-Key"
    return resp


def _sign_in_url(return_path):
    return f"{SITE}/login?redirect={quote(return_path, safe='')}"


def _return_path(slug):
    return f"/listings?l={quote(str(slug or ''), safe='')}"


def _listing_url(slug):
    return SITE + _return_path(slug)


def _verify_url(lead_id):
    return f"{SITE}/listings?verify={quote(lead_id, safe='')}"


def _terms_block():
    return {"version": TERMS_VERSION, "url": TERMS_URL, "summary": TERMS_SUMMARY}


def _program(live_count):
    return {
        "name": PROGRAM_NAME,
        "status": "live" if live_count else "upcoming",
        "headline": PROGRAM_HEADLINE,
        "summary": PROGRAM_SUMMARY,
        "how_it_works": list(PROGRAM_STEPS),
        "note": None if live_count else UPCOMING_NOTE,
        "register_interest": {"method": "POST", "path": "/api/v1/listings/interest",
                              "mcp_tool": "request_listing_intro"},
        "terms": _terms_block(),
    }


def _json_obj(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _num(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def _round2(value):
    n = _num(value)
    return round(n, 2) if n is not None else None


def _iso(value):
    if isinstance(value, datetime):
        return ledger.iso_utc(value)
    return value if isinstance(value, str) else None


def _available(detail):
    for key in ("available", "available_date", "energization", "delivery"):
        val = detail.get(key)
        if isinstance(val, (str, int, float)) and not isinstance(val, bool) and str(val).strip():
            return str(val).strip()[:80]
    return None


def _access(row, v, return_path):
    required = row.get("tier_required") if row.get("tier_required") in _ACCESS_LEVELS else "registered"
    if row.get("status") == "public":
        granted, reason = True, None
    elif not v["identified"]:
        granted, reason = False, (v["reason"] or "sign_in_required")
    elif _TIER_RANK.get(v["tier"], 0) < _REQUIRED_RANK.get(required, 1):
        granted, reason = False, "upgrade_required"
    else:
        granted, reason = True, None
    unlock = None
    if not granted:
        steps = {"sign_in_required": ["claim_free_key", "bind_email"],
                 "email_binding_required": ["bind_email"],
                 "upgrade_required": ["unlock_more_data"]}[reason]
        unlock = {"web_sign_in_url": (None if reason == "upgrade_required"
                                      else _sign_in_url(return_path)),
                  "mcp_steps": steps,
                  "pricing_url": PRICING_URL if reason == "upgrade_required" else None}
    return {"required": required, "granted": granted, "reason": reason, "unlock": unlock}


def _teaser(row, access):
    return {
        "id": row.get("id"),
        "slug": row.get("slug"),
        "title": row.get("title"),
        "summary": row.get("summary"),
        "status": row.get("status"),
        "access_required": access["required"],
        "locked": not access["granted"],
        "market": row.get("market"),
        "state": row.get("state"),
        "country": row.get("country"),
        "capacity_mw": _num(row.get("capacity_mw")),
        "available": _available(_json_obj(row.get("detail"))),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
        "expires_at": _iso(row.get("expires_at")),
        "url": _listing_url(row.get("slug")),
    }


def _full(row, access):
    """Unlocked view. Never `contact` or `owner_id`; coordinates at 2 dp
    (about a kilometre) — the site itself is what the introduction is for."""
    item = _teaser(row, access)
    detail = _json_obj(row.get("detail"))
    item.update({
        "latitude": _round2(row.get("latitude")),
        "longitude": _round2(row.get("longitude")),
        "asking_price": _num(row.get("asking_price")),
        "asking_currency": row.get("asking_currency"),
        "detail": {k: val for k, val in detail.items()
                   if isinstance(k, str) and not k.startswith("_")
                   and k.lower() not in _DETAIL_PRIVATE_KEYS},
    })
    return item


def _listing_ref(row):
    return {"id": row.get("id"), "slug": row.get("slug"), "title": row.get("title")} if row else None


def _entry(row):
    return ledger.check_entry(row, None)["entry"]


def _opening(events):
    return next((e for e in events if e.get("event") in ledger.LEAD_OPENING_EVENTS), None)


def _first(events, name):
    return next((e for e in events if e.get("event") == name), None)


def _requirement_of(row):
    req = row.get("requirement")
    if isinstance(req, str):
        try:
            req = json.loads(req)
        except Exception:
            req = None
    return req if isinstance(req, dict) else None


def _requirement_key(req):
    return ledger.canonical_json(req or {})


def _rate_ok(bucket, key, rule):
    limit, window = rule
    now = time.time()
    k = (bucket, key or "?")
    with _RATE_LOCK:
        if len(_RATE) > 20000:
            _RATE.clear()
        hits = [t for t in _RATE.get(k, []) if now - t < window]
        if len(hits) >= limit:
            _RATE[k] = hits
            return False, int(window - (now - hits[0])) + 1
        hits.append(now)
        _RATE[k] = hits
        return True, 0


_RATE = {}
_RATE_LOCK = threading.Lock()
_LAST_CONFIRM_SENT = {}


# ═════════════════════════════════════════════════════════════════════════
#  input
# ═════════════════════════════════════════════════════════════════════════

def _line(value, limit):
    """Single-line text: None when empty, False when not text."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return False
    text = " ".join(str(value).split())
    return text[:limit] if text else None


def _para(value, limit):
    if value is None:
        return None
    if not isinstance(value, str):
        return False
    text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    return text[:limit] if text else None


def _string_list(value, max_items, item_limit):
    if value is None:
        return []
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        return False
    out = []
    for item in value:
        text = _line(item, item_limit)
        if text is False:
            return False
        if text and text not in out:
            out.append(text)
    return out[:max_items]


def _clean_lead_fields(body, need_requirement):
    problems = {}
    name = _line(body.get("name"), 120)
    company = _line(body.get("company"), 160)
    role = _line(body.get("role"), 120)
    message = _para(body.get("message"), 2000)
    if not name:
        problems["name"] = "required"
    if not company:
        problems["company"] = "required"
    if role is False:
        problems["role"] = "must be text"
    if message is False:
        problems["message"] = "must be text"

    raw_req = body.get("requirement")
    requirement = {}
    if raw_req is not None and not isinstance(raw_req, dict):
        problems["requirement"] = "must be an object"
        raw_req = {}
    raw_req = raw_req or {}
    mw = raw_req.get("capacity_mw")
    if mw not in (None, ""):
        n = _num(mw)
        if n is None or not (0 < n <= 5000):
            problems["requirement.capacity_mw"] = "must be a number of MW between 0 and 5000"
        else:
            requirement["capacity_mw"] = round(n, 3)
    for key, max_items, limit in (("markets", 10, 60), ("states", 15, 40)):
        vals = _string_list(raw_req.get(key), max_items, limit)
        if vals is False:
            problems[f"requirement.{key}"] = "must be a list of names"
        elif vals:
            requirement[key] = vals
    for key, limit in (("timeline", 80), ("use_case", 120)):
        val = _line(raw_req.get(key), limit)
        if val is False:
            problems[f"requirement.{key}"] = "must be text"
        elif val:
            requirement[key] = val
    notes = _para(raw_req.get("notes"), 2000)
    if notes is False:
        problems["requirement.notes"] = "must be text"
    elif notes:
        requirement["notes"] = notes
    if need_requirement and not any(k in requirement for k in ("capacity_mw", "markets", "states")):
        problems.setdefault("requirement", "give at least one of capacity_mw, markets or states")

    client = {}
    raw_client = body.get("client")
    if isinstance(raw_client, dict):
        for key in ("name", "platform", "source"):
            val = _line(raw_client.get(key), 60)
            if val:
                client[key] = _TOKEN_CHARS_RE.sub("", val.lower())[:60] or None
        client = {k: val for k, val in client.items() if val}

    return {"name": name or None, "company": company or None,
            "role": role or None, "message": message or None,
            "requirement": requirement, "client": client or None}, problems


# ═════════════════════════════════════════════════════════════════════════
#  email
# ═════════════════════════════════════════════════════════════════════════

def _resend_sender():
    """main._resend_email — the sanctioned transactional sender — looked up on
    the ALREADY-LOADED app module. Never imported: importing main in a test or
    script boots the whole app."""
    for name in ("main", "__main__"):
        mod = sys.modules.get(name)
        fn = getattr(mod, "_resend_email", None) if mod is not None else None
        if callable(fn):
            return fn
    return None


def _send_email(to_email, subject, html_body):
    sender = _resend_sender()
    if sender is None or not to_email:
        logger.warning("[pocket-listings] no email sender available")
        return False
    try:
        return bool(sender(to_email, subject, html_body, from_name="DC Hub Listings"))
    except Exception as exc:
        logger.warning("[pocket-listings] send failed: %s", exc)
        return False


def _run_bounded(fn, timeout_s):
    """Run fn on a daemon thread; wait up to timeout_s. (finished, result)."""
    box = {}

    def target():
        try:
            box["result"] = fn()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[pocket-listings] background task failed: %s", exc)
            box["result"] = None

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        return False, None
    return True, box.get("result")


def _dispatch(fn):
    threading.Thread(target=fn, daemon=True).start()


def _e(value, default="—"):
    text = str(value) if value not in (None, "") else default
    return html.escape(text, quote=True)


def _hdr(value):
    return " ".join(str(value or "").split())[:180]


def _admin_inbox():
    return (os.environ.get("DCHUB_ADMIN_EMAIL") or os.environ.get("ADMIN_INBOX_EMAIL")
            or "azmartone@gmail.com").strip()


def _requirement_html(req):
    req = req or {}
    rows = []
    labels = (("capacity_mw", "Capacity (MW)"), ("markets", "Markets"),
              ("states", "States"), ("timeline", "Timeline"),
              ("use_case", "Use case"), ("notes", "Notes"))
    for key, label in labels:
        val = req.get(key)
        if val in (None, "", []):
            continue
        if isinstance(val, list):
            val = ", ".join(str(x) for x in val)
        rows.append(f"<tr><td style='padding:2px 12px 2px 0;color:#555'>{_e(label)}</td>"
                    f"<td>{_e(val).replace(chr(10), '<br>')}</td></tr>")
    return f"<table>{''.join(rows)}</table>" if rows else ""


def _confirm_email(lead_id, token, name, listing, requirement):
    url = f"{SITE}/listings?confirm={quote(lead_id, safe='')}&token={quote(token, safe='')}"
    if listing:
        what = f"an introduction to the operator of <b>{_e(listing.get('title'))}</b>"
        subject = _hdr(f"Confirm your DC Hub introduction request — {listing.get('title') or lead_id}")
    else:
        what = "first access to off-market data center capacity matching your requirement"
        subject = "Confirm your DC Hub pocket-listing requirement"
    body = (
        f"<p>Hi {_e(name, 'there')},</p>"
        f"<p>You asked DC Hub for {what}. Confirm the request so DC Hub can register it:</p>"
        f"<p><a href=\"{html.escape(url, quote=True)}\" style=\"display:inline-block;"
        f"padding:10px 18px;background:#6366f1;color:#ffffff;border-radius:6px;"
        f"text-decoration:none\">Confirm my request</a></p>"
        f"<p>Lead reference: <b>{_e(lead_id)}</b></p>"
        f"{_requirement_html(requirement)}"
        f"<p>Once you confirm, DC Hub records the request in its lead register, "
        f"shares your name, company, role and requirement with the operator, and "
        f"makes the introduction. Operator contact details are never published.</p>"
        f"<p style='color:#555;font-size:13px'><b>Introduction terms "
        f"(version {_e(TERMS_VERSION)}):</b> {_e(TERMS_SUMMARY)} "
        f"<a href=\"{html.escape(TERMS_URL, quote=True)}\">Full terms</a></p>"
        f"<p style='color:#555;font-size:13px'>This link expires in 14 days. If you "
        f"did not make this request, ignore this email — nothing is shared until "
        f"it is confirmed.</p>"
    )
    return subject, body


def _lead_facts(events):
    opening = _opening(events) or {}
    entry = _entry(opening) if opening else {}
    confirmed = _first(events, "email_confirmed")
    return opening, entry, confirmed


def _admin_email(lead_id, events, listing_row):
    opening, entry, confirmed = _lead_facts(events)
    title = (listing_row or {}).get("title") or "standing requirement"
    subject = _hdr(f"Pocket listing lead {lead_id} — {opening.get('company') or '?'} → {title}")
    email = opening.get("email") or ""
    mailto = quote(email, safe="@.+-_")
    head = events[-1] if events else {}
    body = (
        f"<h2>Pocket listing lead {_e(lead_id)}</h2>"
        f"<p><b>Status:</b> {_e(ledger.lead_status(events))}<br>"
        f"<b>Listing:</b> {_e(title)} {_e((listing_row or {}).get('slug'), '')}</p>"
        f"<p><b>Name:</b> {_e(opening.get('name'))}<br>"
        f"<b>Email:</b> <a href=\"mailto:{mailto}\">{_e(email)}</a><br>"
        f"<b>Company:</b> {_e(opening.get('company'))}<br>"
        f"<b>Role:</b> {_e(opening.get('role'))}</p>"
        f"{_requirement_html(_requirement_of(opening))}"
        f"<p><b>Message:</b><br>{_e(opening.get('message')).replace(chr(10), '<br>')}</p>"
        f"<p><b>Channel:</b> {_e(entry.get('channel'))} · <b>Platform:</b> "
        f"{_e(entry.get('platform'))} · <b>Client:</b> {_e(json.dumps(entry.get('client') or {}))}<br>"
        f"<b>Registered:</b> {_e(entry.get('created_at'))}<br>"
        f"<b>Confirmed:</b> {_e(_entry(confirmed).get('created_at') if confirmed else None, 'verified at request')}</p>"
        f"<p><b>Verify:</b> {_e(_verify_url(lead_id))}<br>"
        f"<b>Latest ledger entry:</b> #{_e(head.get('seq'))} {_e(head.get('entry_hash'))}</p>"
        f"<p style='color:#555;font-size:12px'>Send the operator notice: POST "
        f"/api/v1/admin/listings/leads/{_e(lead_id)}/notify-operator · mark introduced: "
        f"POST /api/v1/admin/listings/leads/{_e(lead_id)}/status {{\"status\":\"introduced\"}}</p>"
    )
    return subject, body


def _operator_email(lead_id, events, listing_row):
    opening, entry, confirmed = _lead_facts(events)
    title = listing_row.get("title") or listing_row.get("slug")
    subject = _hdr(f"Lead registration notice {lead_id} — {title}")
    entries = "".join(
        f"<tr><td style='padding:2px 12px 2px 0'>#{_e(ev.get('seq'))}</td>"
        f"<td style='padding:2px 12px 2px 0'>{_e(ev.get('event'))}</td>"
        f"<td style='padding:2px 12px 2px 0'>{_e(_entry(ev).get('created_at'))}</td>"
        f"<td style='font-family:monospace;font-size:12px'>{_e(ev.get('entry_hash'))}</td></tr>"
        for ev in events)
    channel = entry.get("channel")
    via = ("an AI agent" + (f" ({entry.get('platform')})" if entry.get("platform") else "")
           if channel == "mcp" else "dchub.cloud")
    body = (
        f"<p>DC Hub has registered the following prospect for your listing "
        f"<b>{_e(title)}</b>.</p>"
        f"<p><b>Lead:</b> {_e(lead_id)}<br>"
        f"<b>Prospect:</b> {_e(opening.get('name'))}, {_e(opening.get('role'), '')} "
        f"{_e(opening.get('company'))}<br>"
        f"<b>Email domain:</b> {_e(entry.get('email_domain'))}<br>"
        f"<b>Arrived via:</b> {_e(via)}<br>"
        f"<b>Registered (UTC):</b> {_e(entry.get('created_at'))}<br>"
        f"<b>Inbox confirmed (UTC):</b> "
        f"{_e(_entry(confirmed).get('created_at') if confirmed else None, 'verified at request')}</p>"
        f"{_requirement_html(_requirement_of(opening))}"
        f"<p><b>Register entries for this lead</b></p><table>{entries}</table>"
        f"<p>Check the live record at any time: {_e(_verify_url(lead_id))}. Keep "
        f"this email: its receipt time and the hashes above are your own record "
        f"that DC Hub registered this prospect for your listing.</p>"
        f"<p>DC Hub will make the introduction. Reply to {_e(SUPPORT_EMAIL)} "
        f"about this lead.</p>"
    )
    return subject, body


def _operator_address(contact):
    for key in ("notify_email", "email"):
        val = str(contact.get(key) or "").strip()
        if "@" in val:
            return val
    return None


def _notify_operator(lead_id, listing_row, secret, events, to=None):
    contact = _json_obj(listing_row.get("contact"))
    address = to or _operator_address(contact)
    if not address:
        return {"sent": False, "reason": "no_operator_address", "ledger": None}
    subject, body = _operator_email(lead_id, events, listing_row)
    if not _send_email(address, subject, body):
        return {"sent": False, "reason": "send_failed", "ledger": None}
    opening = _opening(events) or {}
    rec = _append_event(secret=secret, lead_id=lead_id, event="operator_notified",
                        listing=_listing_ref(listing_row), user_ref=opening.get("user_ref"),
                        email_verified=True, channel="admin",
                        meta={"recipient_domain": ledger.email_domain(address)})
    return {"sent": True, "reason": None,
            "ledger": {"seq": rec["seq"], "entry_hash": rec["entry_hash"]}}


def _on_registered(lead_id, listing_row, secret):
    """A lead just became real (inbox proven). Admin always; the operator only
    when the listing opts in with contact.auto_notify."""
    def work():
        events = _db_lead_events(lead_id)
        subject, body = _admin_email(lead_id, events, listing_row)
        _send_email(_admin_inbox(), subject, body)
        if listing_row:
            contact = _json_obj(listing_row.get("contact"))
            if contact.get("auto_notify") is True and _operator_address(contact):
                _notify_operator(lead_id, listing_row, secret, events)
    _dispatch(work)


def _send_confirmation(secret, lead_id, commitment, email, name, listing, requirement):
    token = ledger.confirm_token(secret, lead_id, commitment)
    subject, body = _confirm_email(lead_id, token, name, listing, requirement)
    finished, sent = _run_bounded(lambda: _send_email(email, subject, body), _SEND_WAIT_S)
    _LAST_CONFIRM_SENT[lead_id] = time.time()
    expires = datetime.now(timezone.utc) + timedelta(seconds=ledger.CONFIRM_TOKEN_TTL_S)
    return {"required": True, "sent": bool(sent) if finished else None,
            "queued": not finished, "sent_to": ledger.mask_email(email),
            "expires_at": ledger.iso_utc(expires)}


# ═════════════════════════════════════════════════════════════════════════
#  lead registration (shared by /intro and /interest)
# ═════════════════════════════════════════════════════════════════════════

def _register_lead(row):
    """-> {"_error": response} or the success fields. `row` None = standing
    requirement."""
    v = _viewer()
    return_path = _return_path(row.get("slug")) if row else "/listings"
    if not v["identified"]:
        steps = (["bind_email"] if v["reason"] == "email_binding_required"
                 else ["claim_free_key", "bind_email"])
        return {"_error": _err(
            401, "identity_required",
            "Sign in to request an introduction — or, from an AI agent, use a key "
            "with your human's email bound (claim_free_key, then bind_email) or an "
            "OAuth connection.",
            reason=v["reason"] or "sign_in_required",
            access={"required": "registered", "granted": False,
                    "reason": v["reason"] or "sign_in_required",
                    "unlock": {"web_sign_in_url": _sign_in_url(return_path),
                               "mcp_steps": steps, "pricing_url": None}})}
    if row is not None:
        acc = _access(row, v, return_path)
        if not acc["granted"]:
            return {"_error": _err(403, "upgrade_required",
                                   "This listing is open to a higher plan.", access=acc)}

    body = request.get_json(silent=True)
    body = body if isinstance(body, dict) else {}
    fields, problems = _clean_lead_fields(body, need_requirement=row is None)
    if problems:
        return {"_error": _err(422, "invalid_request", "Some fields need attention.",
                               fields=problems)}
    if body.get("accept_terms") is not True:
        return {"_error": _err(422, "terms_not_accepted",
                               "Read and accept the introduction terms first.",
                               terms=_terms_block())}
    offered = body.get("terms_version")
    if offered not in (None, "") and str(offered) != TERMS_VERSION:
        return {"_error": _err(409, "terms_version_mismatch",
                               "The introduction terms have changed — review them again.",
                               terms=_terms_block())}
    allowed, retry = _rate_ok("lead", v["user_ref"], _LEAD_RATE_LIMIT)
    if not allowed:
        return {"_error": _err(429, "rate_limited", "Too many requests — try again later.",
                               retry_after_s=retry)}
    secret = ledger.ledger_secret()
    if secret is None:
        logger.error("[pocket-listings] no ledger secret — refusing to register a lead")
        return {"_error": _err(503, "ledger_unavailable",
                               f"Requests cannot be registered right now. Email {SUPPORT_EMAIL}.")}

    event = "intro_requested" if row is not None else "interest_registered"
    listing = _listing_ref(row)
    pii = {"email": v["email"], "name": fields["name"], "role": fields["role"],
           "company": fields["company"], "message": fields["message"],
           "requirement": fields["requirement"]}

    try:
        events = _find_duplicate(v, event, row, fields["requirement"])
    except Exception as exc:
        logger.warning("[pocket-listings] duplicate check failed: %s", exc)
        events = None
    duplicate = events is not None

    if duplicate:
        opening = _opening(events)
        lead_id = opening["lead_id"]
        status = ledger.lead_status(events)
        head = events[-1]
        confirmation = {"required": status == "pending_email_confirmation", "sent": False,
                        "queued": False, "sent_to": ledger.mask_email(opening.get("email")),
                        "expires_at": None}
        if (status == "pending_email_confirmation"
                and time.time() - _LAST_CONFIRM_SENT.get(lead_id, 0) > _RESEND_AFTER_S):
            confirmation = _send_confirmation(
                secret, lead_id, _entry(opening).get("pii_commitment"), opening.get("email"),
                opening.get("name"), listing, _requirement_of(opening))
        registered_at = _entry(opening).get("created_at")
        ledger_ref = {"seq": head.get("seq"), "entry_hash": head.get("entry_hash")}
    else:
        verified_via = None
        try:
            verified_via = _db_verified_via(v)
        except Exception as exc:
            logger.warning("[pocket-listings] verification lookup failed: %s", exc)
        lead_id = ledger.new_lead_id()
        try:
            rec = _append_event(
                secret=secret, lead_id=lead_id, event=event, listing=listing,
                user_ref=v["user_ref"], pii=pii, email_verified=bool(verified_via),
                verified_via=verified_via, channel=v["channel"], platform=v["platform"],
                client=fields["client"], session_hash=v["session_hash"], ip_hash=_ip_hash(),
                user_agent=_user_agent(), terms_version=TERMS_VERSION,
                meta={"kind": "listing_introduction" if row else "standing_requirement"})
        except ledger.LedgerUnavailable as exc:
            logger.error("[pocket-listings] ledger write failed: %s", exc)
            return {"_error": _err(503, "ledger_unavailable",
                                   f"Requests cannot be registered right now. Email {SUPPORT_EMAIL}.")}
        registered_at = rec["created_at"]
        ledger_ref = {"seq": rec["seq"], "entry_hash": rec["entry_hash"]}
        if verified_via:
            status = "registered"
            confirmation = {"required": False, "sent": False, "queued": False,
                            "sent_to": None, "expires_at": None}
            _on_registered(lead_id, row, secret)
        else:
            status = "pending_email_confirmation"
            confirmation = _send_confirmation(secret, lead_id, rec["pii_commitment"], v["email"],
                                              fields["name"], listing, fields["requirement"])

    masked = ledger.mask_email(v["email"])
    if status == "pending_email_confirmation":
        if confirmation.get("sent") is False and confirmation.get("required") and not duplicate:
            nxt = (f"The request is recorded as {lead_id}, but the confirmation email "
                   f"could not be sent. Try again shortly or email {SUPPORT_EMAIL}.")
        else:
            nxt = (f"A confirmation link was sent to {masked}. The request is registered "
                   f"once it is confirmed; DC Hub then makes the introduction.")
    elif status == "withdrawn":
        nxt = f"This request was withdrawn. Email {SUPPORT_EMAIL} to reopen it."
    elif row is not None:
        nxt = "Registered. DC Hub will introduce you to the operator and email you when it does."
    else:
        nxt = "Registered. DC Hub will email you when a matching listing opens."

    return {"lead_id": lead_id, "status": status, "duplicate": duplicate,
            "listing": {"slug": listing["slug"], "title": listing["title"]} if listing else None,
            "registered_at": registered_at, "email_masked": masked,
            "confirmation": confirmation, "verify_url": _verify_url(lead_id),
            "ledger": ledger_ref, "next": nxt, "requirement": fields["requirement"]}


def _find_duplicate(v, event, row, requirement):
    since = datetime.now(timezone.utc) - _DUP_WINDOW[event]
    candidates = _db_user_openings(v["user_ref"], event, row.get("id") if row else None, since)
    for cand in reversed(candidates):
        if (event == "interest_registered"
                and _requirement_key(_requirement_of(cand)) != _requirement_key(requirement)):
            continue
        events = _db_lead_events(cand["lead_id"])
        if ledger.lead_status(events) != "withdrawn":
            return events
    return None


def _safe_get_listing(slug_or_id):
    if str(slug_or_id or "").strip().lower() in _RESERVED_SLUGS:
        return None
    return _db_get_listing(slug_or_id)


# ═════════════════════════════════════════════════════════════════════════
#  public routes
# ═════════════════════════════════════════════════════════════════════════

@exclusive_listings_bp.route("/api/v1/listings", methods=["GET"])
def list_listings():
    """Teaser feed. Everyone sees every live listing's teaser; nobody sees
    operator contact. ?market= ?state= ?min_mw= ?limit= (default 50, max 200)."""
    _ensure_schema()
    v = _viewer()
    market = (request.args.get("market") or "").strip()[:80]
    state = (request.args.get("state") or "").strip().upper()[:40]
    min_mw = _num(request.args.get("min_mw"))
    try:
        limit = max(1, min(int(request.args.get("limit", "50")), 200))
    except ValueError:
        limit = 50
    try:
        rows = _db_list_listings(market=market or None, state=state or None,
                                 min_mw=min_mw, limit=limit)
        live_count = (len(rows) if not (market or state or min_mw is not None)
                      else _db_count_live())
    except Exception as exc:
        logger.warning("[pocket-listings] list failed: %s", exc)
        return jsonify(ok=False, error="listings_unavailable", caller_tier=v["tier"]), 200

    items, needs_upgrade = [], 0
    for row in rows:
        acc = _access(row, v, _return_path(row.get("slug")))
        items.append(_teaser(row, acc))
        if acc["reason"] == "upgrade_required":
            needs_upgrade += 1

    out = {
        "ok": True,
        "program": _program(live_count),
        "viewer": _viewer_public(v, "/listings"),
        "count": len(items),
        "items": items,
        "pocket_locked_count": sum(1 for i in items if i["locked"]),
        "caller_tier": v["tier"],
        "can_see_pocket": bool(v["identified"]),
    }
    if needs_upgrade:
        out["upgrade_for_pocket"] = {
            "tier_required": "pro",
            "url": PRICING_URL,
            "message": f"{needs_upgrade} listing(s) here open on a paid plan.",
        }
    resp = jsonify(out)
    _no_store(resp)
    return resp, 200


@exclusive_listings_bp.route("/api/v1/listings/terms", methods=["GET"])
def listing_terms():
    out = {"ok": True, "terms": {"version": TERMS_VERSION, "url": TERMS_URL,
                                 "summary": TERMS_SUMMARY, "text": TERMS_TEXT}}
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp, 200


@exclusive_listings_bp.route("/api/v1/listings/<slug_or_id>", methods=["GET"])
def get_listing(slug_or_id):
    """One listing. Locked callers get the teaser plus the way in."""
    _ensure_schema()
    v = _viewer()
    try:
        row = _safe_get_listing(slug_or_id)
    except Exception as exc:
        logger.warning("[pocket-listings] detail failed: %s", exc)
        return jsonify(ok=False, error="listing_unavailable", caller_tier=v["tier"]), 200
    if not row or row.get("status") not in ("pocket", "public"):
        return _err(404, "not_found", "No such listing.")

    return_path = _return_path(row.get("slug"))
    access = _access(row, v, return_path)
    if access["granted"] and v["identified"]:
        _record_view(row, v)
    out = {
        "ok": True,
        "locked": not access["granted"],
        "listing": _full(row, access) if access["granted"] else _teaser(row, access),
        "access": access,
        "introduction": {"method": "POST",
                         "path": f"/api/v1/listings/{row.get('slug')}/intro",
                         "mcp_tool": "request_listing_intro",
                         "operator_contact": "never_shared"},
        "viewer": _viewer_public(v, return_path),
        "caller_tier": v["tier"],
    }
    resp = jsonify(out)
    _no_store(resp)
    return resp, 200


def _record_view(row, v):
    """One `listing_viewed` entry per identity per listing per day. Evidence
    that a prospect saw the listing on DC Hub; never blocks the page."""
    secret = ledger.ledger_secret()
    if secret is None:
        return
    try:
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        if _db_recent_view(v["user_ref"], row.get("id"), since):
            return
        _append_event(secret=secret, lead_id=None, event="listing_viewed",
                      listing=_listing_ref(row), user_ref=v["user_ref"],
                      channel=v["channel"], platform=v["platform"],
                      session_hash=v["session_hash"], ip_hash=_ip_hash())
    except Exception as exc:
        logger.warning("[pocket-listings] view not recorded: %s", exc)


@exclusive_listings_bp.route("/api/v1/listings/<slug_or_id>/intro", methods=["POST"])
def request_intro(slug_or_id):
    _ensure_schema()
    try:
        row = _safe_get_listing(slug_or_id)
    except Exception as exc:
        logger.warning("[pocket-listings] intro lookup failed: %s", exc)
        return _err(503, "listing_unavailable", "Listings are unavailable right now.")
    if not row or row.get("status") not in ("pocket", "public"):
        return _err(404, "not_found", "No such listing.")
    result = _register_lead(row)
    if "_error" in result:
        return result["_error"]
    out = {
        "ok": True,
        "lead_id": result["lead_id"],
        "kind": "listing_introduction",
        "status": result["status"],
        "duplicate": result["duplicate"],
        "listing": result["listing"],
        "registered_at": result["registered_at"],
        "email_masked": result["email_masked"],
        "confirmation": result["confirmation"],
        "verify_url": result["verify_url"],
        "ledger": result["ledger"],
        "next": result["next"],
    }
    resp = jsonify(out)
    _no_store(resp)
    return resp, 200


@exclusive_listings_bp.route("/api/v1/listings/interest", methods=["POST"])
def register_interest():
    """A standing requirement — how demand is captured while listings are
    still being onboarded."""
    _ensure_schema()
    result = _register_lead(None)
    if "_error" in result:
        return result["_error"]
    try:
        matching = _db_count_matching(result["requirement"])
    except Exception:
        matching = 0
    out = {
        "ok": True,
        "lead_id": result["lead_id"],
        "kind": "standing_requirement",
        "status": result["status"],
        "duplicate": result["duplicate"],
        "listing": None,
        "registered_at": result["registered_at"],
        "email_masked": result["email_masked"],
        "confirmation": result["confirmation"],
        "verify_url": result["verify_url"],
        "ledger": result["ledger"],
        "matching_listings": matching,
        "next": result["next"],
    }
    resp = jsonify(out)
    _no_store(resp)
    return resp, 200


@exclusive_listings_bp.route("/api/v1/listings/leads/confirm", methods=["POST"])
def confirm_lead():
    """The prospect proves the inbox. POST only: mail scanners prefetch GET
    links, and a prefetch must not register anyone."""
    _ensure_schema()
    body = request.get_json(silent=True)
    body = body if isinstance(body, dict) else {}
    lead_id = str(body.get("lead_id") or "").strip().upper()
    token = str(body.get("token") or "").strip()[:160]
    allowed, retry = _rate_ok("confirm", _ip_hash(), _CONFIRM_RATE_LIMIT)
    if not allowed:
        return _err(429, "rate_limited", "Too many attempts — try again later.",
                    retry_after_s=retry)
    if not ledger.looks_like_lead_id(lead_id) or not token:
        return _err(400, "invalid_token", "This confirmation link is not valid.")
    secret = ledger.ledger_secret()
    if secret is None:
        return _err(503, "ledger_unavailable",
                    f"Confirmations are unavailable right now. Email {SUPPORT_EMAIL}.")
    try:
        events = _db_lead_events(lead_id)
    except Exception as exc:
        logger.warning("[pocket-listings] confirm lookup failed: %s", exc)
        return _err(503, "ledger_unavailable", "Confirmations are unavailable right now.")
    opening = _opening(events)
    if not opening:
        return _err(404, "not_found", "No such request.")
    if not ledger.check_confirm_token(secret, lead_id, _entry(opening).get("pii_commitment"), token):
        return _err(400, "invalid_token", "This confirmation link is not valid or has expired.")
    status = ledger.lead_status(events)
    if status == "withdrawn":
        return _err(409, "lead_withdrawn", f"This request was withdrawn. Email {SUPPORT_EMAIL}.")

    already = status != "pending_email_confirmation"
    if already:
        confirmed = _first(events, "email_confirmed")
        confirmed_at = _entry(confirmed if confirmed else opening).get("created_at")
    else:
        try:
            rec = _append_event(
                secret=secret, lead_id=lead_id, event="email_confirmed",
                listing=({"id": opening.get("listing_id"), "slug": opening.get("listing_slug"),
                          "title": opening.get("listing_title")}
                         if opening.get("listing_id") is not None else None),
                user_ref=opening.get("user_ref"), email_verified=True,
                verified_via="email_link", channel="web", ip_hash=_ip_hash(),
                user_agent=_user_agent(), terms_version=opening.get("terms_version"),
                meta={"confirms_seq": opening.get("seq")})
        except ledger.LedgerUnavailable as exc:
            logger.error("[pocket-listings] confirm write failed: %s", exc)
            return _err(503, "ledger_unavailable", "Confirmations are unavailable right now.")
        confirmed_at = rec["created_at"]
        listing_row = None
        if opening.get("listing_id") is not None:
            try:
                listing_row = _db_get_listing(str(opening["listing_id"]))
            except Exception:
                listing_row = None
        _on_registered(lead_id, listing_row, secret)
        status = "registered"

    out = {
        "ok": True,
        "lead_id": lead_id,
        "status": status,
        "confirmed_at": confirmed_at,
        "already_confirmed": already,
        "verify_url": _verify_url(lead_id),
        "listing": ({"slug": opening.get("listing_slug"), "title": opening.get("listing_title")}
                    if opening.get("listing_id") is not None else None),
    }
    resp = jsonify(out)
    _no_store(resp)
    return resp, 200


@exclusive_listings_bp.route("/api/v1/listings/leads/<lead_id>/verify", methods=["GET"])
def verify_lead(lead_id):
    """Public registration record. Company and email DOMAIN only — never the
    prospect's name or address."""
    _ensure_schema()
    lead_id = str(lead_id or "").strip().upper()
    allowed, retry = _rate_ok("verify", _ip_hash(), _READ_RATE_LIMIT)
    if not allowed:
        return _err(429, "rate_limited", "Too many requests — try again later.",
                    retry_after_s=retry)
    if not ledger.looks_like_lead_id(lead_id):
        return _err(404, "not_found", "No such lead.")
    try:
        events = _db_lead_events(lead_id)
    except Exception as exc:
        logger.warning("[pocket-listings] verify lookup failed: %s", exc)
        return _err(503, "ledger_unavailable", "The register is unavailable right now.")
    opening = _opening(events)
    if not opening:
        return _err(404, "not_found", "No such lead.")

    keys = ledger.ledger_keys()
    chain = _safe_chain_summary()
    first_break = chain.get("first_break_seq")
    event_rows = []
    for ev in events:
        res = ledger.check_entry(ev, keys)
        event_rows.append({
            "seq": ev.get("seq"),
            "event": res["entry"].get("event") or ev.get("event"),
            "at": res["entry"].get("created_at"),
            "entry_hash": ev.get("entry_hash"),
            "prev_hash": ev.get("prev_hash"),
            "hash_valid": res["hash_valid"],
            "signature_valid": res["signature_valid"],
            "columns_consistent": res["columns_consistent"],
            "pii": ledger.pii_state(ev, res["entry"], keys),
            "within_intact_chain": (chain.get("intact") is True
                                    or (first_break is not None and (ev.get("seq") or 0) < first_break)),
        })
    entry = _entry(opening)
    confirmed = _first(events, "email_confirmed")
    out = {
        "ok": True,
        "lead_id": lead_id,
        "issuer": "DC Hub · dchub.cloud",
        "kind": "listing_introduction" if opening.get("event") == "intro_requested" else "standing_requirement",
        "status": ledger.lead_status(events),
        "listing": ({"slug": entry.get("listing_slug"), "title": entry.get("listing_title")}
                    if entry.get("listing_id") is not None else None),
        "prospect": {"company": opening.get("company"),
                     "email_domain": entry.get("email_domain"),
                     "email_verified": bool(entry.get("email_verified") or confirmed),
                     "verified_via": entry.get("verified_via") or ("email_link" if confirmed else None)},
        "channel": entry.get("channel"),
        "platform": entry.get("platform"),
        "registered_at": entry.get("created_at"),
        "confirmed_at": _entry(confirmed).get("created_at") if confirmed else None,
        "events": event_rows,
        "chain": chain,
        "how_to_verify": HOW_TO_VERIFY,
    }
    resp = jsonify(out)
    _no_store(resp)
    return resp, 200


@exclusive_listings_bp.route("/api/v1/listings/<slug_or_id>/leads", methods=["GET"])
def operator_ledger(slug_or_id):
    """The operator's view of registered leads for one listing (?token= from
    the admin operator-link endpoint). Email addresses appear only once DC Hub
    has made the introduction."""
    _ensure_schema()
    token = (request.args.get("token") or "").strip()[:200]
    allowed, retry = _rate_ok("ledger", _ip_hash(), _READ_RATE_LIMIT)
    if not allowed:
        return _err(429, "rate_limited", "Too many requests — try again later.",
                    retry_after_s=retry)
    secret = ledger.ledger_secret()
    if secret is None:
        return _err(503, "ledger_unavailable", "The register is unavailable right now.")
    try:
        row = _safe_get_listing(slug_or_id)
    except Exception:
        return _err(503, "listing_unavailable", "Listings are unavailable right now.")
    if not row or not ledger.check_operator_token(secret, row.get("id") or 0, token):
        return _err(403, "invalid_token", "This ledger link is not valid or has expired.")
    try:
        events = _db_listing_events(row["id"])
    except Exception:
        return _err(503, "ledger_unavailable", "The register is unavailable right now.")

    grouped = {}
    for ev in events:
        if ev.get("lead_id"):
            grouped.setdefault(ev["lead_id"], []).append(ev)
    leads = []
    for lead_id, evs in grouped.items():
        opening = _opening(evs)
        status = ledger.lead_status(evs)
        if not opening or status in (None, "pending_email_confirmation"):
            continue        # an unconfirmed request is not a registration yet
        entry = _entry(opening)
        confirmed = _first(evs, "email_confirmed")
        introduced = _first(evs, "introduced") is not None
        leads.append({
            "lead_id": lead_id,
            "status": status,
            "registered_at": entry.get("created_at"),
            "confirmed_at": _entry(confirmed).get("created_at") if confirmed else None,
            "name": opening.get("name"),
            "company": opening.get("company"),
            "role": opening.get("role"),
            "email_domain": entry.get("email_domain"),
            "email": opening.get("email") if introduced else None,
            "requirement": _requirement_of(opening),
            "channel": entry.get("channel"),
            "platform": entry.get("platform"),
            "verify_url": _verify_url(lead_id),
        })
    views = [ev for ev in events if ev.get("event") == "listing_viewed"]
    out = {
        "ok": True,
        "listing": {"slug": row.get("slug"), "title": row.get("title")},
        "leads": leads,
        "identified_views": {"count": len(views),
                             "unique_viewers": len({ev.get("user_ref") for ev in views}),
                             "last_viewed_at": _entry(views[-1]).get("created_at") if views else None},
        "chain": _safe_chain_summary(),
        "generated_at": ledger.iso_utc(datetime.now(timezone.utc)),
    }
    resp = jsonify(out)
    _no_store(resp)
    return resp, 200


# ═════════════════════════════════════════════════════════════════════════
#  admin
# ═════════════════════════════════════════════════════════════════════════

def _admin_denied():
    return _err(401, "unauthorized", "X-Admin-Key header required.")


def _safe_slug(s):
    return _SLUG_RE.sub("-", (s or "").strip().lower()).strip("-")[:80]


@exclusive_listings_bp.route("/api/v1/admin/listings", methods=["GET", "POST"])
def admin_listings():
    """GET: every listing including drafts and operator contact.
    POST: create (status defaults to draft, tier_required to registered)."""
    if not _admin_ok():
        return _admin_denied()
    _ensure_schema()
    if request.method == "GET":
        try:
            rows = _db_all_listings()
        except Exception as exc:
            return _err(503, "listings_unavailable", str(exc)[:200])
        for r in rows:
            for k in ("created_at", "updated_at", "expires_at"):
                r[k] = _iso(r.get(k))
            for k in ("capacity_mw", "asking_price", "latitude", "longitude"):
                r[k] = _num(r.get(k))
        return jsonify({"ok": True, "count": len(rows), "listings": rows}), 200

    body = request.get_json(silent=True) or {}
    title = (body.get("title") or "").strip()
    if not title:
        return _err(400, "invalid_request", "title required")
    slug = _safe_slug(body.get("slug") or title) or "site-" + secrets.token_hex(3)
    if slug in _RESERVED_SLUGS:
        return _err(400, "invalid_request", f"'{slug}' is a reserved path word")
    status = (body.get("status") or "draft").lower()
    if status not in _VALID_STATUSES:
        return _err(400, "invalid_request", "invalid status", allowed=list(_VALID_STATUSES))
    tier_required = (body.get("tier_required") or "registered").lower()
    if tier_required not in _ACCESS_LEVELS:
        return _err(400, "invalid_request", "invalid tier_required",
                    allowed=list(_ACCESS_LEVELS))
    detail = body.get("detail")
    contact = body.get("contact")
    try:
        c = _conn()
        try:
            with c.cursor() as cur:
                cur.execute(
                    """INSERT INTO exclusive_listings
                           (slug, title, summary, status, tier_required,
                            market, state, country, latitude, longitude,
                            capacity_mw, asking_price, asking_currency,
                            detail, contact, owner_id, expires_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)
                       ON CONFLICT (slug) DO NOTHING
                       RETURNING id, slug""",
                    (slug, title, body.get("summary"), status, tier_required,
                     body.get("market"), body.get("state"), body.get("country", "US"),
                     body.get("latitude"), body.get("longitude"), body.get("capacity_mw"),
                     body.get("asking_price"), body.get("asking_currency", "USD"),
                     json.dumps(detail) if detail is not None and not isinstance(detail, str) else detail,
                     json.dumps(contact) if contact is not None and not isinstance(contact, str) else contact,
                     body.get("owner_id"), body.get("expires_at")))
                got = cur.fetchone()
            c.commit()
        finally:
            _close(c)
    except Exception as exc:
        return _err(503, "write_failed", str(exc)[:200])
    if not got:
        return _err(409, "slug_exists", "slug already exists", slug=slug)
    return jsonify({"ok": True, "id": got[0], "slug": got[1], "status": status,
                    "tier_required": tier_required}), 200


@exclusive_listings_bp.route("/api/v1/admin/listings/<int:lid>",
                             methods=["PUT", "PATCH", "DELETE"])
def update_or_delete_listing(lid):
    """PUT/PATCH = partial update; promote with {"status": "pocket"} and open
    with {"status": "public"}. DELETE removes the listing (ledger entries keep
    their own slug/title snapshot). One route for all three keeps the
    duplicate-route lint happy."""
    if not _admin_ok():
        return _admin_denied()
    _ensure_schema()
    if request.method == "DELETE":
        try:
            c = _conn()
            try:
                with c.cursor() as cur:
                    cur.execute("DELETE FROM exclusive_listings WHERE id = %s", (lid,))
                    n = cur.rowcount
                c.commit()
            finally:
                _close(c)
        except Exception as exc:
            return _err(503, "write_failed", str(exc)[:200])
        return jsonify({"ok": True, "deleted": n}), 200

    body = request.get_json(silent=True) or {}
    if "status" in body and body["status"] not in _VALID_STATUSES:
        return _err(400, "invalid_request", "invalid status", allowed=list(_VALID_STATUSES))
    if "tier_required" in body and body["tier_required"] not in _ACCESS_LEVELS:
        return _err(400, "invalid_request", "invalid tier_required",
                    allowed=list(_ACCESS_LEVELS))
    settable = ("title", "summary", "status", "tier_required", "market", "state",
                "country", "latitude", "longitude", "capacity_mw", "asking_price",
                "asking_currency", "owner_id", "expires_at")
    fields, values = [], []
    for key in settable:
        if key in body:
            fields.append(f"{key} = %s")
            values.append(body[key])
    for key in ("detail", "contact"):
        if key in body:
            fields.append(f"{key} = %s::jsonb")
            val = body[key]
            values.append(val if isinstance(val, str) or val is None else json.dumps(val))
    if not fields:
        return _err(400, "invalid_request", "no fields to update")
    fields.append("updated_at = NOW()")
    try:
        c = _conn()
        try:
            with c.cursor() as cur:
                cur.execute(f"UPDATE exclusive_listings SET {', '.join(fields)} "
                            f"WHERE id = %s RETURNING id, slug, status, tier_required",
                            values + [lid])
                got = cur.fetchone()
            c.commit()
        finally:
            _close(c)
    except Exception as exc:
        return _err(503, "write_failed", str(exc)[:200])
    if not got:
        return _err(404, "not_found", "No such listing.")
    return jsonify({"ok": True, "id": got[0], "slug": got[1], "status": got[2],
                    "tier_required": got[3]}), 200


@exclusive_listings_bp.route("/api/v1/admin/listings/leads", methods=["GET"])
def admin_leads():
    """Ledger rows with prospect PII, newest first. ?listing_id= ?lead_id= ?limit="""
    if not _admin_ok():
        return _admin_denied()
    _ensure_schema()
    try:
        listing_id = int(request.args["listing_id"]) if request.args.get("listing_id") else None
        limit = max(1, min(int(request.args.get("limit", "200")), 2000))
    except ValueError:
        return _err(400, "invalid_request", "listing_id and limit must be integers")
    lead_id = (request.args.get("lead_id") or "").strip().upper() or None
    try:
        rows = _db_admin_ledger(listing_id=listing_id, lead_id=lead_id, limit=limit)
    except Exception as exc:
        return _err(503, "ledger_unavailable", str(exc)[:200])
    by_lead = {}
    for r in sorted(rows, key=lambda x: x.get("seq") or 0):
        if r.get("lead_id"):
            by_lead.setdefault(r["lead_id"], []).append(r)
    entries = []
    for r in rows:
        entries.append({k: (_iso(r[k]) if k == "created_at" else r[k])
                        for k in _LEDGER_COLS if k != "entry_json"})
    statuses = {lid: ledger.lead_status(evs) for lid, evs in by_lead.items()}
    return jsonify({"ok": True, "count": len(entries), "entries": entries,
                    "lead_statuses": statuses}), 200


@exclusive_listings_bp.route("/api/v1/admin/listings/leads/<lead_id>/notify-operator",
                             methods=["POST"])
def admin_notify_operator(lead_id):
    if not _admin_ok():
        return _admin_denied()
    _ensure_schema()
    lead_id = str(lead_id or "").strip().upper()
    secret = ledger.ledger_secret()
    if secret is None:
        return _err(503, "ledger_unavailable", "no ledger secret configured")
    try:
        events = _db_lead_events(lead_id)
    except Exception as exc:
        return _err(503, "ledger_unavailable", str(exc)[:200])
    opening = _opening(events)
    if not opening or opening.get("listing_id") is None:
        return _err(404, "not_found", "No introduction request with that id.")
    if ledger.lead_status(events) in (None, "pending_email_confirmation", "withdrawn"):
        return _err(409, "not_registered",
                    "Only a confirmed, open lead can be sent to an operator.")
    try:
        listing_row = _db_get_listing(str(opening["listing_id"]))
    except Exception as exc:
        return _err(503, "listing_unavailable", str(exc)[:200])
    if not listing_row:
        return _err(404, "not_found", "The listing no longer exists.")
    body = request.get_json(silent=True) or {}
    to = str(body.get("to") or "").strip() or None
    if to and "@" not in to:
        return _err(400, "invalid_request", "to must be an email address")
    finished, result = _run_bounded(
        lambda: _notify_operator(lead_id, listing_row, secret, events, to=to), 12.0)
    if not finished:
        return jsonify({"ok": True, "queued": True, "sent": None}), 202
    result = result or {"sent": False, "reason": "send_failed", "ledger": None}
    return jsonify({"ok": bool(result.get("sent")), "queued": False,
                    "sent": bool(result.get("sent")), "reason": result.get("reason"),
                    "ledger": result.get("ledger")}), 200


@exclusive_listings_bp.route("/api/v1/admin/listings/leads/<lead_id>/status",
                             methods=["POST"])
def admin_lead_status(lead_id):
    if not _admin_ok():
        return _admin_denied()
    _ensure_schema()
    lead_id = str(lead_id or "").strip().upper()
    body = request.get_json(silent=True) or {}
    new_status = str(body.get("status") or "").strip().lower()
    if new_status not in ("introduced", "withdrawn"):
        return _err(400, "invalid_request", "status must be introduced or withdrawn")
    secret = ledger.ledger_secret()
    if secret is None:
        return _err(503, "ledger_unavailable", "no ledger secret configured")
    try:
        events = _db_lead_events(lead_id)
    except Exception as exc:
        return _err(503, "ledger_unavailable", str(exc)[:200])
    opening = _opening(events)
    if not opening:
        return _err(404, "not_found", "No such lead.")
    current = ledger.lead_status(events)
    if current == "withdrawn":
        return _err(409, "lead_withdrawn", "This lead was withdrawn.")
    if new_status == "introduced" and current == "pending_email_confirmation":
        return _err(409, "not_registered", "The prospect has not confirmed the request.")
    try:
        rec = _append_event(
            secret=secret, lead_id=lead_id, event=new_status,
            listing=({"id": opening.get("listing_id"), "slug": opening.get("listing_slug"),
                      "title": opening.get("listing_title")}
                     if opening.get("listing_id") is not None else None),
            user_ref=opening.get("user_ref"), email_verified=True, channel="admin")
    except ledger.LedgerUnavailable as exc:
        return _err(503, "ledger_unavailable", str(exc)[:200])
    return jsonify({"ok": True, "lead_id": lead_id, "status": new_status,
                    "ledger": {"seq": rec["seq"], "entry_hash": rec["entry_hash"]}}), 200


@exclusive_listings_bp.route("/api/v1/admin/listings/<int:lid>/operator-link",
                             methods=["POST"])
def admin_operator_link(lid):
    if not _admin_ok():
        return _admin_denied()
    _ensure_schema()
    secret = ledger.ledger_secret()
    if secret is None:
        return _err(503, "ledger_unavailable", "no ledger secret configured")
    try:
        row = _db_get_listing(str(lid))
    except Exception as exc:
        return _err(503, "listing_unavailable", str(exc)[:200])
    if not row:
        return _err(404, "not_found", "No such listing.")
    token = ledger.operator_token(secret, row["id"])
    expires = datetime.now(timezone.utc) + timedelta(seconds=ledger.OPERATOR_TOKEN_TTL_S)
    return jsonify({
        "ok": True,
        "listing": {"id": row["id"], "slug": row.get("slug"), "title": row.get("title")},
        "url": f"{SITE}/listings?ledger={quote(str(row.get('slug')), safe='')}&token={quote(token, safe='')}",
        "api": f"/api/v1/listings/{row.get('slug')}/leads?token={quote(token, safe='')}",
        "expires_at": ledger.iso_utc(expires),
    }), 200


@exclusive_listings_bp.route("/api/v1/admin/listings/ledger/verify", methods=["GET"])
def admin_verify_ledger():
    if not _admin_ok():
        return _admin_denied()
    _ensure_schema()
    secret = ledger.ledger_secret()
    try:
        summary = _chain_summary(force=True)
    except Exception as exc:
        return _err(503, "ledger_unavailable", str(exc)[:200])
    return jsonify({"ok": True, "chain": summary,
                    "signing_key_configured": secret is not None,
                    "key_id": ledger.key_id(secret) if secret else None}), 200


@exclusive_listings_bp.route("/api/v1/listings/health", methods=["GET"])
def listings_health():
    _ensure_schema()
    try:
        c = _conn()
        try:
            with c.cursor() as cur:
                cur.execute(
                    """SELECT status, COUNT(*) FROM exclusive_listings
                        GROUP BY status""")
                by_status = {r[0]: int(r[1]) for r in cur.fetchall()}
        finally:
            _close(c)
        return jsonify(status="ok", by_status=by_status), 200
    except Exception as e:
        return jsonify(status="error", error=str(e)[:200]), 200
