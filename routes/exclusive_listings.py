"""
exclusive_listings.py — DC Hub Capacity Source (Pocket Listings until 2026-09-13): data center capacity,
an auth wall, and a lead register operators can verify.

Phase GG (2026-05-14) created the table and a Pro-only feed that handed Pro
callers the operator's contact details. 2026-09-11 rebuilt the program around
what it actually has to prove: that a prospect reached the operator THROUGH
DC Hub.

  * AUTH WALL, not a paywall, by default. `tier_required` is `registered`
    unless an admin sets `pro` / `enterprise` / `founding` on a listing.
    Teasers (market, state, capacity, status) are public — they are the demand
    driver. Full detail needs an identified END USER: a signed-in web user, an
    agent key with an email bound, or an OAuth-connected agent. That user
    accepts the introduction terms once, before the first walled listing opens,
    and the acceptance is recorded in the lead register with the terms version;
    an introduction or requirement registered under the current version counts.
  * SPECS, NO IDENTITY (2026-09-15). An open listing is its specs: market,
    size, schedule, delivery type, power stage, price, verification. The
    provider's name (unless provider.disclosed), the site (`site`, legacy
    address-like detail keys, coordinates, substation) and the provider's
    contact are held back until the provider accepts THAT viewer's
    registration; get_listing then returns them in `disclosure`.
  * DEAL REGISTRATION. A registration (POST .../intro) reaches the provider as
    the buyer's company name and requirement only. The provider accepts or
    declines it (POST .../leads/<lead_id>/decision, with its ledger token). On
    accept, DC Hub emails each side the other's contact and the buyer the site
    identity; on decline, no contact details go either way. Every decision is
    a ledger entry, and _decide is the one place that records it.
  * SEARCH. The feed filters by size (min_kw, min_mw) and location (region,
    country, location), every filter in the parameterised WHERE clause. Regions
    are derived from canonical_stats' country map, so they agree with DC Hub's
    own region stats.
  * Every request, identified view, confirmation, operator notice and decision
    is an entry in util/listing_ledger.py's hash chain.
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
    GET  /api/v1/listings                          teaser feed + program + viewer + filters
    GET  /api/v1/listings/terms                    introduction terms
    POST /api/v1/listings/terms/accept             accept them, once per terms version
    GET  /api/v1/listings/health
    GET  /api/v1/listings/summary                  live listings by market and delivery type
    GET  /api/v1/listings/<slug_or_id>             specs (walled) + the viewer's identity block
    POST /api/v1/listings/<slug_or_id>/intro       register for a listing
    POST /api/v1/listings/interest                 register a requirement
    POST /api/v1/listings/leads/confirm            prospect confirms from inbox
    GET  /api/v1/listings/leads/<lead_id>/verify   public registration record
    GET  /api/v1/listings/<slug_or_id>/leads       operator ledger (?token=)
    POST /api/v1/listings/<slug_or_id>/leads/<lead_id>/decision
                                                   operator accepts or declines (token in body)
  admin (X-Admin-Key):
    GET  /api/v1/admin/listings                    every listing incl. contact
    POST /api/v1/admin/listings                    create
    PUT|PATCH|DELETE /api/v1/admin/listings/<id>
    GET  /api/v1/admin/listings/leads              ledger rows with PII
    POST /api/v1/admin/listings/leads/<lead_id>/notify-operator
    POST /api/v1/admin/listings/leads/<lead_id>/status
                                                   accepted | declined | introduced | withdrawn
    POST /api/v1/admin/listings/<id>/operator-link
    GET  /api/v1/admin/listings/ledger/verify

Listing `contact` (admin-only JSON): {name, title, company, email, phone,
notify_email, auto_notify, co_marketing}. With auto_notify true, a confirmed
lead also sends the operator a registration notice; otherwise the admin sends
it. Once the operator accepts a registration, the buyer gets name, title, email
and phone (notify_email only when it is the only address). co_marketing
({linkedin_post_url, posted_at, website_url}) records the provider's
co-marketing and is served only to admin and the tokenized operator ledger.

Listing `detail` (JSON) holds free-form keys plus nine RESERVED keys, all
optional: colocation, delivery_type, mw_schedule, power, price, provider,
site, update_cadence, verification. colocation belongs only to a listing whose
delivery_type is colocation. Admin writes validate the reserved keys and store
them normalized; a failure answers 400 invalid_detail with one entry per field
and writes nothing (_validate_detail). Reads project them as typed fields:
every teaser carries delivery_type, update_cadence, freshness (from
verification.verified_at) and the provider's name when it is disclosed; the
specs view adds colocation, mw_schedule, power without its substation, price,
provider and verification. The generic `detail` object never repeats a reserved
key or an address-like identity key (_IDENTITY_DETAIL_KEYS), so an undisclosed
provider name and the site are served only in a released `disclosure` block.
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
from urllib.parse import quote, urlsplit

from flask import Blueprint, jsonify, request

# Regions are derived from DC Hub's own country -> region map, so a Capacity
# Source region and a region in DC Hub's published stats name the same
# countries. location_names.US_STATES is the state map the location pages use.
from canonical_stats import _COUNTRY_NAME, _COUNTRY_REGION, _REGION_ORDER
from location_names import US_STATES
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
TERMS_VERSION = "2026-09-15"
TERMS_URL = f"{SITE}/listings#terms"
TERMS_SUMMARY = (
    "DC Hub records your registration in its lead register and sends the "
    "operator your company name and stated requirement so it can accept or "
    "decline the registration. Only if the operator accepts does DC Hub share "
    "your name, role, email and message with the operator, and the operator's "
    "identity, site details and contact with you. If it declines, neither "
    "side's contact details are shared. For 12 months you agree to pursue a "
    "listed opportunity through DC Hub rather than approaching its operator "
    "directly."
)
TERMS_TEXT = (
    "DC Hub Capacity Source — introduction terms (version " + TERMS_VERSION + ")\n\n"
    "1. What DC Hub does. DC Hub publishes data center capacity on behalf of "
    "operators, including capacity that is not publicly marketed, and "
    "introduces prospects to them. DC Hub is not a "
    "party to any agreement between you and an operator.\n\n"
    "2. Registration. When you request an introduction or register a "
    "requirement, DC Hub records who asked, when, for which listing and through "
    "which channel (the website or an AI agent) in a tamper-evident lead "
    "register, and asks you to confirm the request from your inbox. DC Hub "
    "sends the operator your company name and stated requirement so it can "
    "accept or decline the registration. Only if the operator accepts does DC "
    "Hub share your name, role, email address and message with the operator, "
    "and the operator's identity, site details and contact with you. If the "
    "operator declines, neither side's contact details are shared.\n\n"
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

PROGRAM_NAME = "DC Hub Capacity Source"
PROGRAM_HEADLINE = "The live source for data center capacity"
PROGRAM_SUMMARY = (
    "Powered land, powered shells and turnkey capacity, including sites that "
    "are not publicly marketed, for enterprise buyers and the AI agents that "
    "procure for them. Search by size and location; every listing shows when "
    "it was last updated. Sign in and accept the introduction terms once to "
    "see a listing's specs, then register for it: when the provider accepts, "
    "DC Hub shares the site details and contacts."
)
PROGRAM_STEPS = (
    "Search listings by size (kW or MW) and location — region, country, state "
    "or market — and see when each was last updated, without an account.",
    "Sign in with a free account, or connect an identified AI agent, and "
    "accept the introduction terms once to see a listing's specs.",
    "Register for a listing. DC Hub sends the provider your company name and "
    "requirement; when the provider accepts, DC Hub shares the site details "
    "and contacts with both sides.",
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

# ★ Listing data is shared to evaluate ONE opportunity under the terms above,
# not published. Every response says so in machine-readable form, because the
# MCP gateway keeps a backend-supplied provenance/citation block over its own
# default CC-BY-4.0 grant (dchub-mcp-server lib/attribution.mjs mergeProvenance /
# reconcileCitation). Without these, every agent reading a Capacity Source listing would
# be told it may republish it.
# Only `citation` is emitted. `provenance` is a data-CURRENCY claim key to
# scripts/dataset_inventory.py, and listings are curated inventory, not an
# ingested feed with a freshness to watch; the MCP tools force a matching
# confidential provenance on their side (dchub-mcp-server _listingConfidential).
LISTING_LICENSE = "LicenseRef-DCHub-Capacity-Source-Confidential"
LISTING_CITE_AS = ("DC Hub Capacity Source (confidential — not for redistribution), "
                   "dchub.cloud")


def _citation():
    return {"source": "DC Hub Capacity Source", "url": SITE + "/listings",
            "license": LISTING_LICENSE, "license_url": TERMS_URL,
            "redistribution": "not_permitted", "cite_as": LISTING_CITE_AS}


_ACCESS_LEVELS = ("registered", "pro", "enterprise", "founding")
_REQUIRED_RANK = {"registered": 1, "pro": 3, "founding": 3, "enterprise": 4}
_TIER_RANK = {"anonymous": 0, "": 0, "free": 1, "identified": 1,
              "registered": 1, "starter": 1, "developer": 2, "pro": 3,
              "founding": 3, "team": 3, "paid": 3, "metered": 3,
              "enterprise": 4, "admin": 4}
_VALID_STATUSES = ("draft", "pocket", "public")
# Path words under /api/v1/listings/ that must never become a listing slug.
_RESERVED_SLUGS = frozenset({"interest", "leads", "terms", "health", "admin",
                             "verify", "confirm", "intro", "summary"})
_SLUG_RE = re.compile(r"[^a-z0-9-]+")
_TOKEN_CHARS_RE = re.compile(r"[^a-z0-9._/-]+")
_DETAIL_PRIVATE_KEYS = frozenset({"contact", "operator_contact", "operator_email",
                                  "notify_email", "internal", "internal_notes",
                                  "owner", "owner_id"})

# Reserved `detail` keys: validated on admin write (_DETAIL_FIELD_CHECKS),
# projected as typed fields by _teaser / _full, and left out of the generic
# `detail` object.
_DETAIL_RESERVED_KEYS = ("colocation", "delivery_type", "mw_schedule", "power",
                         "price", "provider", "site", "update_cadence", "verification")
_SITE_KEYS = ("name", "address", "city", "postal_code", "parcel_id")
_UPDATE_CADENCES = ("real_time", "weekly", "monthly")
# freshness.overdue once verification.verified_at is more than this many days old.
_CADENCE_OVERDUE_DAYS = {"real_time": 2, "weekly": 9, "monthly": 35}
_CADENCE_PHRASE = {"real_time": "in real time", "weekly": "every week", "monthly": "every month"}
# Free-form `detail` keys that identify the site or its provider, matched
# ignoring case and surrounding whitespace. The specs view never serves them; a
# released identity block reads each as the `site` field it names, after the
# reserved `site` itself and in this order.
_IDENTITY_DETAIL_KEYS = {
    "site_name": "name", "facility_name": "name",
    "address": "address", "street_address": "address", "site_address": "address",
    "city": "city", "provider_city": "city",
    "postal_code": "postal_code", "zip": "postal_code",
    "parcel_id": "parcel_id", "parcel": "parcel_id", "apn": "parcel_id",
}
_DELIVERY_TYPES = ("land", "powered_shell", "turnkey", "colocation")
_INTERCONNECTION_STAGES = ("not_started", "applied", "in_study",
                           "agreement_executed", "under_construction", "energized")
_PRICE_UNITS = ("usd_per_kw_month", "usd_per_mw", "usd_per_acre", "usd_total")
_VERIFICATION_METHODS = ("provider_attestation", "document_review",
                         "utility_confirmation", "site_visit")
_FIELD_TEXT_MAX = 120
_MW_SCHEDULE_MAX_ENTRIES = 24
_MW_SCHEDULE_MAX_MW = 10000
_COLOCATION_MAX_KW = 100000
_COLOCATION_MAX_KW_PER_CABINET = 300
_VERIFIED_AT_MAX_AHEAD = timedelta(days=1)
# Freshness, by whole days since verification.verified_at: up to
# _FRESH_MAX_AGE_DAYS is fresh, up to _AGING_MAX_AGE_DAYS is aging, older is
# stale.
_FRESH_MAX_AGE_DAYS = 30
_AGING_MAX_AGE_DAYS = 90
_SCHEDULE_DATE_RE = re.compile(r"([0-9]{4})-([0-9]{2})(?:-([0-9]{2}))?")
_VERIFIED_AT_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})"
    r"(?:[Tt ]([0-9]{2}):([0-9]{2})(?::([0-9]{2})(?:\.([0-9]{1,6}))?)?"
    r"([Zz]|[+-][0-9]{2}:?[0-9]{2})?)?")

# ── search: regions ───────────────────────────────────────────────────────
REGION_KEYS = ("north_america", "latin_america", "europe", "asia_pacific",
               "middle_east_africa")
_REGION_LABELS = {"north_america": "North America", "latin_america": "Latin America",
                  "europe": "Europe", "asia_pacific": "Asia-Pacific",
                  "middle_east_africa": "Middle East and Africa"}
# canonical_stats names six regions for prose (_REGION_ORDER). Capacity Source
# searches five: four map one to one, and "the Middle East" and "Africa" share
# middle_east_africa. tests pin that every _REGION_ORDER label is mapped.
_CANONICAL_REGION_KEY = {"North America": "north_america", "Latin America": "latin_america",
                         "Europe": "europe", "Asia-Pacific": "asia_pacific",
                         "the Middle East": "middle_east_africa", "Africa": "middle_east_africa"}
# A region value, lowercased with runs of spaces, hyphens and underscores read
# as one underscore (_region_token), -> the keys it stands for.
_REGION_ALIASES = {
    "north_america": ("north_america",), "na": ("north_america",),
    "latin_america": ("latin_america",), "latam": ("latin_america",),
    "south_america": ("latin_america",),
    "europe": ("europe",),
    "asia_pacific": ("asia_pacific",), "apac": ("asia_pacific",),
    "middle_east_africa": ("middle_east_africa",), "mea": ("middle_east_africa",),
    "middle_east": ("middle_east_africa",), "africa": ("middle_east_africa",),
    "emea": ("europe", "middle_east_africa"),
    "americas": ("north_america", "latin_america"),
}


def _build_region_tables():
    """ISO-2 code -> region key, lowercased country name -> region key, and
    lowercased name -> its first ISO-2 code, from canonical_stats' maps."""
    code_region, name_region, code_by_name = {}, {}, {}
    for name, label in _COUNTRY_REGION.items():
        if label in _CANONICAL_REGION_KEY:
            name_region[name.lower()] = _CANONICAL_REGION_KEY[label]
    for code, name in _COUNTRY_NAME.items():
        code_by_name.setdefault(name.lower(), code)
        if name.lower() in name_region:
            code_region[code.upper()] = name_region[name.lower()]
    return code_region, name_region, code_by_name


_COUNTRY_CODE_REGION, _COUNTRY_NAME_REGION, _COUNTRY_CODE_BY_NAME = _build_region_tables()
_STATE_CODE_BY_NAME = {name.lower(): code for code, name in US_STATES.items()}
_SEARCH_MAX_VALUES = 20
_SEARCH_VALUE_MAX = 80
_MARKET_SUBSTRING_MIN = 3
_REQUIREMENT_MAX_KW = 5_000_000

# ── deal registration ─────────────────────────────────────────────────────
_DECISIONS = ("accept", "decline")
_DECISION_EVENT = {"accept": "registration_accepted", "decline": "registration_declined"}
_DECISION_STATUS = {"accept": "accepted", "decline": "declined"}
# A provider can accept or decline only while a confirmed registration awaits it.
_DECIDABLE_STATUSES = ("registered", "operator_notified")
# The statuses that share the buyer's contact with the provider and the site
# identity with the buyer.
_RELEASED_STATUSES = ("accepted", "introduced")
_DECISION_NOTE_MAX = 500
_DISCLOSURE_STATUS = {"pending_email_confirmation": "pending", "registered": "pending",
                    "operator_notified": "pending", "declined": "declined",
                    "accepted": "accepted", "introduced": "introduced"}

# Before acceptance, a requirement's free text reaches the provider with email
# addresses, links and phone numbers taken out.
_SCRUB_TEXT_FIELDS = ("timeline", "use_case", "notes")
_SCRUB_EMAIL_RE = re.compile(r"[^\s@<>()\[\]{}\"',;:]+@[^\s@<>()\[\]{}\"',;:]+")
_SCRUB_URL_RE = re.compile(
    r"(?i)(?:\b[a-z][a-z0-9+.-]*://|\bwww\.)\S+"
    r"|\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+"
    r"(?:com|net|org|io|ai|co|us|uk|ca|de|fr|nl|au|in|jp|sg|ie|eu|dev|app|cloud|biz|"
    r"info|tech|xyz|me|energy|capital|partners|group|global|digital|systems|solutions|"
    r"network|holdings|ventures|services|link|site|online)\b(?:/\S*)?")
_SCRUB_PHONE_RE = re.compile(r"(?<![\w+(])[+(]?(?:\d[\s().-]{0,2}){6,14}\d(?!\w)")
_SCRUB_LOCAL_PHONE_RE = re.compile(r"(?<!\d)\d{3}[\s.-]\d{4}$")

# ── provider co-marketing ─────────────────────────────────────────────────
_CO_MARKETING_KEYS = ("linkedin_post_url", "posted_at", "website_url")
_LINKEDIN_HOSTS = ("linkedin.com", "www.linkedin.com")
_URL_MAX = 500
_POSTED_AT_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")

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


# The listings the public feed shows. Every read of live listings filters on
# these, so the feed, its counts and the summary describe the same set.
_LIVE_WHERE = ("status IN ('public', 'pocket')",
               "(expires_at IS NULL OR expires_at > NOW())")


# ── search predicates ─────────────────────────────────────────────────────
# ONE matcher for size and ONE for place, shared by the feed filters
# (_db_list_listings) and the standing-requirement count (_db_count_matching).
# No predicate contains " AND " at its top level, so a WHERE clause joined with
# " AND " splits back into its predicates.

# Stored `country` is mostly an ISO-2 code but admin input is free text, so a
# country matches by code (upper case) or by name (lower case), spaces trimmed.
_COUNTRY_MATCH_SQL = ("(UPPER(TRIM(country)) = ANY(%s::text[]) "
                      "OR LOWER(TRIM(country)) = ANY(%s::text[]))")
_LOCATION_MATCH_SQL = ("(UPPER(TRIM(country)) = ANY(%s::text[]) "
                       "OR LOWER(TRIM(country)) = ANY(%s::text[]) "
                       "OR UPPER(TRIM(state)) = ANY(%s::text[]) "
                       "OR LOWER(TRIM(state)) = ANY(%s::text[]) "
                       "OR market ILIKE ANY(%s::text[]))")


def _size_sql(min_kw, unknown_matches=False):
    """-> (predicate, params): the listing offers at least min_kw kW. A
    colocation listing is sized by detail.colocation.kw_available, cast only
    when it is a JSON number; any other listing by capacity_mw * 1000.
    unknown_matches decides a listing whose size is not recorded: the feed
    leaves it out, a requirement count keeps it as a possible match."""
    unknown = "TRUE" if unknown_matches else "FALSE"
    sql = ("(CASE WHEN detail->>'delivery_type' = 'colocation' THEN "
           "CASE WHEN jsonb_typeof(detail->'colocation'->'kw_available') = 'number' "
           "THEN (detail->'colocation'->>'kw_available')::numeric >= %s::numeric "
           f"ELSE {unknown} END "
           f"WHEN capacity_mw IS NULL THEN {unknown} "
           "ELSE capacity_mw::numeric * 1000 >= %s::numeric END)")
    return sql, [min_kw, min_kw]


def _region_token(value):
    """'North-America', 'north america' and 'NORTH_AMERICA' -> 'north_america'."""
    return re.sub(r"[\s_-]+", "_", str(value).strip().lower()).strip("_")


def _region_keys(values):
    """-> (the region keys `values` name, as keys or aliases, in REGION_KEYS
    order; the values that name no region)."""
    keys, unknown = set(), []
    for value in values:
        found = _REGION_ALIASES.get(_region_token(value))
        if found:
            keys.update(found)
        else:
            unknown.append(value)
    return [k for k in REGION_KEYS if k in keys], unknown


def _region_of(country):
    """The region key of a stored `country`, or None when canonical_stats does
    not place it. Reads the value as _COUNTRY_MATCH_SQL does: spaces trimmed, a
    code by upper case, a name by lower case."""
    if not isinstance(country, str):
        return None
    text = country.strip(" ")
    if not text:
        return None
    return _COUNTRY_CODE_REGION.get(text.upper()) or _COUNTRY_NAME_REGION.get(text.lower())


def _region_countries(keys):
    """[ISO-2 codes, lowercased country names] of the countries in these regions."""
    wanted = set(keys)
    return [sorted(c for c, k in _COUNTRY_CODE_REGION.items() if k in wanted),
            sorted(n for n, k in _COUNTRY_NAME_REGION.items() if k in wanted)]


def _country_values(values):
    """country values -> (ISO-2 codes, lowercased names, normalized labels). A
    two-letter value is a code, and also matches the name canonical_stats gives
    that code when it is the name's first code; a name canonical_stats knows
    also matches its code; any other text matches stored text equal to it."""
    codes, names, labels = set(), set(), []
    for value in values:
        text = " ".join(str(value).split())
        if not text:
            continue
        if len(text) == 2 and text.isalpha():
            label = text.upper()
            codes.add(label)
            name = _COUNTRY_NAME.get(label)
            if name and _COUNTRY_CODE_BY_NAME.get(name.lower()) == label:
                names.add(name.lower())
        else:
            names.add(text.lower())
            code = _COUNTRY_CODE_BY_NAME.get(text.lower())
            if code:
                codes.add(code)
            label = code or text
        if label not in labels:
            labels.append(label)
    return sorted(codes), sorted(names), labels


def _like_escape(text):
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _location_params(terms):
    """location terms -> the five arrays _LOCATION_MATCH_SQL binds. A term
    matches a region name or alias, a country code or name, a US state code or
    name, or a market containing it (terms of 3+ characters; LIKE wildcards in
    the term are escaped). A listing matches when ANY term does."""
    codes, names, state_codes, state_names, patterns = set(), set(), set(), set(), []
    for term in terms:
        keys, _ = _region_keys([term])
        region_codes, region_names = _region_countries(keys)
        country_codes, country_names, _ = _country_values([term])
        codes.update(region_codes, country_codes)
        names.update(region_names, country_names)
        low = term.lower()
        state_names.add(low)
        if len(term) == 2 and term.isalpha():
            state_codes.add(term.upper())
            if term.upper() in US_STATES:
                state_names.add(US_STATES[term.upper()].lower())
        if low in _STATE_CODE_BY_NAME:
            state_codes.add(_STATE_CODE_BY_NAME[low])
        if len(term) >= _MARKET_SUBSTRING_MIN:
            pattern = "%" + _like_escape(low) + "%"
            if pattern not in patterns:
                patterns.append(pattern)
    return [sorted(codes), sorted(names), sorted(state_codes), sorted(state_names), patterns]


def _db_list_listings(market=None, state=None, min_mw=None, delivery_type=None,
                      available_by=None, min_kw=None, regions=None, countries=None,
                      location=None, limit=50):
    """Live listings, newest first. delivery_type matches detail.delivery_type
    exactly. available_by ('YYYY-MM' or 'YYYY-MM-DD') keeps listings with at
    least one detail.mw_schedule entry dated in or before that month. min_kw is
    _size_sql; regions are region keys, countries country values
    (_country_values), location terms any one of which matches
    (_location_params). The families AND together. Every filter is part of the
    WHERE clause, so LIMIT counts matching rows only."""
    where = list(_LIVE_WHERE)
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
    if min_kw is not None:
        size_sql, size_params = _size_sql(min_kw)
        where.append(size_sql)
        params += size_params
    if regions:
        where.append(_COUNTRY_MATCH_SQL)
        params += _region_countries(regions)
    if countries:
        codes, names, _ = _country_values(countries)
        where.append(_COUNTRY_MATCH_SQL)
        params += [codes, names]
    if location:
        where.append(_LOCATION_MATCH_SQL)
        params += _location_params(location)
    if delivery_type:
        where.append("detail->>'delivery_type' = %s")
        params.append(delivery_type)
    if available_by:
        # The CASE hands jsonb_array_elements an empty array when a stored
        # mw_schedule is not an array. An entry matches on its YYYY-MM prefix,
        # compared byte-wise; a date not shaped YYYY-MM[-DD] never matches.
        where.append(
            "EXISTS (SELECT 1 FROM jsonb_array_elements(CASE WHEN "
            "jsonb_typeof(detail->'mw_schedule') = 'array' "
            "THEN detail->'mw_schedule' ELSE '[]'::jsonb END) AS schedule(entry) "
            "WHERE substring(entry->>'date' FROM '^([0-9]{4}-[0-9]{2})(-[0-9]{2})?$') "
            "COLLATE \"C\" <= %s)")
        params.append(str(available_by)[:7])
    sql = (f"SELECT {', '.join(_LISTING_COLS)} FROM exclusive_listings "
           f"WHERE {' AND '.join(where)} ORDER BY updated_at DESC LIMIT %s")
    return _fetch(sql, params + [limit], _LISTING_COLS)


def _db_count_live():
    rows = _fetch(f"SELECT COUNT(*) FROM exclusive_listings WHERE {' AND '.join(_LIVE_WHERE)}",
                  [], ("n",))
    return int(rows[0]["n"] or 0) if rows else 0


_SUMMARY_COLS = ("market", "state", "country", "capacity_mw", "delivery_type", "updated_at")


def _db_live_listing_facts():
    """Market, capacity, delivery type and last update of every live listing,
    newest first: the teaser-level columns the summary aggregates."""
    return _fetch("SELECT market, state, country, capacity_mw, detail->>'delivery_type', "
                  "updated_at FROM exclusive_listings "
                  f"WHERE {' AND '.join(_LIVE_WHERE)} ORDER BY updated_at DESC",
                  [], _SUMMARY_COLS)


def _db_count_matching(requirement):
    """Live listings a standing requirement could match: in ANY of its places
    (markets, states, regions, countries) and of its size. Regions, countries
    and capacity_kw use the feed's own matchers (_COUNTRY_MATCH_SQL,
    _size_sql); a listing whose size is not recorded counts as a possible
    match, as it always has for capacity_mw."""
    where = list(_LIVE_WHERE)
    params = []
    places, place_params = [], []
    markets = [m.lower() for m in requirement.get("markets") or []]
    states = [s.upper() for s in requirement.get("states") or []]
    if markets or states:
        places.append("LOWER(market) = ANY(%s) OR UPPER(state) = ANY(%s)")
        place_params += [markets, states]
    if requirement.get("regions"):
        places.append(_COUNTRY_MATCH_SQL)
        place_params += _region_countries(requirement["regions"])
    if requirement.get("countries"):
        codes, names, _ = _country_values(requirement["countries"])
        places.append(_COUNTRY_MATCH_SQL)
        place_params += [codes, names]
    if places:
        where.append("(" + " OR ".join(places) + ")")
        params += place_params
    if requirement.get("capacity_mw") is not None:
        where.append("(capacity_mw IS NULL OR capacity_mw >= %s)")
        params.append(requirement["capacity_mw"])
    if requirement.get("capacity_kw") is not None:
        size_sql, size_params = _size_sql(requirement["capacity_kw"], unknown_matches=True)
        where.append(size_sql)
        params += size_params
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


_VIEWER_LEAD_COLS = ("seq", "lead_id", "event", "listing_id", "user_ref",
                     "email_verified", "created_at", "entry_json")


def _db_viewer_lead_events(user_ref, listing_id):
    """Every ledger event of the registrations `user_ref` opened for this
    listing, of any age, oldest first."""
    return _fetch("SELECT seq, lead_id, event, listing_id, user_ref, email_verified, created_at, "
                  "entry_json FROM listing_lead_ledger WHERE lead_id IN (SELECT lead_id "
                  "FROM listing_lead_ledger WHERE user_ref = %s AND listing_id = %s "
                  "AND event = 'intro_requested') ORDER BY seq ASC LIMIT 2000",
                  [user_ref, listing_id], _VIEWER_LEAD_COLS)


def _db_provider_live_countries(provider_name):
    """`country` of every live listing whose detail.provider.name is this
    name, ignoring case."""
    rows = _fetch(f"SELECT country FROM exclusive_listings WHERE {' AND '.join(_LIVE_WHERE)} "
                  "AND LOWER(detail->'provider'->>'name') = LOWER(%s)",
                  [provider_name], ("country",))
    return [r["country"] for r in rows]


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


def _db_terms_accepted(user_ref, version):
    """Has this identity accepted the introduction terms under `version`? An
    introduction or requirement registered under that version carried the
    acceptance itself, so it counts too."""
    rows = _fetch("SELECT seq FROM listing_lead_ledger WHERE user_ref = %s "
                  "AND terms_version = %s AND event IN ('terms_accepted', "
                  "'intro_requested', 'interest_registered') LIMIT 1",
                  [user_ref, version], ("seq",))
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

def _err(http_status, code, message, **extra):
    """An error answer. `extra` becomes top-level keys, so a caller can pass
    status= — a lead's status, say — without colliding with the HTTP status."""
    body = {"ok": False, "error": code, "message": message,
            "citation": _citation()}
    body.update(extra)
    resp = jsonify(body)
    resp.headers["Cache-Control"] = "private, no-store"
    return resp, http_status


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
                              "mcp_tool": "request_capacity_intro"},
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


def _available(detail, schedule):
    """The listing's own availability wording: the first of available,
    available_date, energization and delivery that holds a value. Without one,
    the earliest date of `schedule` (the checked mw_schedule) as stored, such
    as "2026-12"; otherwise None."""
    for key in ("available", "available_date", "energization", "delivery"):
        val = detail.get(key)
        if isinstance(val, (str, int, float)) and not isinstance(val, bool) and str(val).strip():
            return str(val).strip()[:80]
    return schedule[0]["date"] if schedule else None


# ═════════════════════════════════════════════════════════════════════════
#  listing fields (reserved `detail` keys)
# ═════════════════════════════════════════════════════════════════════════

def _now():
    """The clock the listing-field rules read: the verified_at bound on
    write, and freshness on read."""
    return datetime.now(timezone.utc)


def _json_number(n):
    """A finite float as the JSON number to store: an int when it is whole."""
    return int(n) if n.is_integer() and abs(n) < 2 ** 53 else n


def _field_text(value, limit):
    """Required single-line text: whitespace runs collapse to one space, then
    1..limit characters. -> (text, None) or (None, problem)."""
    if value is None:
        return None, "is required"
    if not isinstance(value, str):
        return None, "must be text"
    text = " ".join(value.split())
    if not text:
        return None, "must not be empty"
    if len(text) > limit:
        return None, f"must be at most {limit} characters"
    return text, None


def _field_choice(value, allowed):
    """-> (value, None) when the trimmed text is one of `allowed`, else
    (None, problem)."""
    if isinstance(value, str) and value.strip() in allowed:
        return value.strip(), None
    prefix = "is required: one of " if value is None else "must be one of "
    return None, prefix + ", ".join(allowed)


def _schedule_date(value):
    """A trimmed 'YYYY-MM' or 'YYYY-MM-DD' that names a real calendar date,
    else None."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    m = _SCHEDULE_DATE_RE.fullmatch(text)
    if not m:
        return None
    try:
        datetime(int(m.group(1)), int(m.group(2)), int(m.group(3) or 1))
    except ValueError:
        return None
    return text


def _parse_verified_at(value):
    """An ISO-8601 date or date-time as an aware UTC datetime, else None. A
    date is midnight UTC; a date-time without an offset is UTC."""
    if not isinstance(value, str):
        return None
    m = _VERIFIED_AT_RE.fullmatch(value.strip())
    if not m:
        return None
    year, month, day, hour, minute, second, fraction, offset = m.groups()
    try:
        tz = timezone.utc
        if offset and offset not in ("Z", "z"):
            digits = offset[1:].replace(":", "")
            if int(digits[2:]) > 59:
                return None
            shift = timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
            tz = timezone(-shift if offset[0] == "-" else shift)
        parsed = datetime(int(year), int(month), int(day), int(hour or 0),
                          int(minute or 0), int(second or 0),
                          int((fraction or "0").ljust(6, "0")), tzinfo=tz)
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _no_other_keys(obj, allowed, path, errors, what):
    for key in obj:
        if key not in allowed:
            errors.append({"field": f"{path}.{key}",
                           "message": f"is not a field of {what} ({', '.join(allowed)})"})


# Each check appends {"field", "message"} entries to `errors` and returns the
# normalized value, or None when it appended anything.

def _check_colocation(value, path, errors, now):
    """The colocation space on offer: kw_available, plus cabinets_available
    and max_kw_per_cabinet when known. Only a colocation listing carries it
    (_colocation_allowed)."""
    start = len(errors)
    if not isinstance(value, dict):
        errors.append({"field": path, "message": (
            "must be an object with kw_available, and optionally cabinets_available "
            "and max_kw_per_cabinet")})
        return None
    _no_other_keys(value, ("kw_available", "cabinets_available", "max_kw_per_cabinet"),
                   path, errors, "colocation")
    out = {}
    kw = _num(value.get("kw_available"))
    if kw is None or not 0 < kw <= _COLOCATION_MAX_KW:
        errors.append({"field": f"{path}.kw_available", "message": (
            ("is required: " if value.get("kw_available") is None else "must be ")
            + f"a number greater than 0 and at most {_COLOCATION_MAX_KW}")})
    else:
        out["kw_available"] = kw
    if value.get("cabinets_available") is not None:
        cabinets = _num(value["cabinets_available"])
        if cabinets is None or cabinets < 0 or not cabinets.is_integer():
            errors.append({"field": f"{path}.cabinets_available",
                           "message": "must be a whole number of at least 0"})
        else:
            out["cabinets_available"] = cabinets
    if value.get("max_kw_per_cabinet") is not None:
        density = _num(value["max_kw_per_cabinet"])
        if density is None or not 0 < density <= _COLOCATION_MAX_KW_PER_CABINET:
            errors.append({"field": f"{path}.max_kw_per_cabinet", "message": (
                "must be a number greater than 0 and at most "
                f"{_COLOCATION_MAX_KW_PER_CABINET}")})
        else:
            out["max_kw_per_cabinet"] = density
    if len(errors) > start:
        return None
    return {key: _json_number(n) for key, n in out.items()}


def _colocation_allowed(delivery_type):
    """colocation describes colocation space, so it belongs only to a listing
    whose delivery_type is colocation: refused on write, None on read."""
    return delivery_type == "colocation"


def _check_delivery_type(value, path, errors, now):
    choice, problem = _field_choice(value, _DELIVERY_TYPES)
    if problem:
        errors.append({"field": path, "message": problem})
    return choice


def _check_mw_schedule(value, path, errors, now):
    """1..24 {date, mw} entries: cumulative MW available by each date, so
    stored sorted by date with MW that never falls. A month entry (YYYY-MM)
    covers every day in it, so it may not share its month with another entry."""
    start = len(errors)
    if not isinstance(value, list) or not 1 <= len(value) <= _MW_SCHEDULE_MAX_ENTRIES:
        errors.append({"field": path, "message": (
            f"must be a list of 1 to {_MW_SCHEDULE_MAX_ENTRIES} entries of {{date, mw}}")})
        return None
    entries = []
    for i, raw in enumerate(value):
        at = f"{path}[{i}]"
        if not isinstance(raw, dict):
            errors.append({"field": at, "message": "must be an object with date and mw"})
            continue
        _no_other_keys(raw, ("date", "mw"), at, errors, "an mw_schedule entry")
        date, mw = _schedule_date(raw.get("date")), _num(raw.get("mw"))
        if date is None:
            errors.append({"field": f"{at}.date",
                           "message": "must be a calendar date as YYYY-MM or YYYY-MM-DD"})
        if mw is None or not 0 < mw <= _MW_SCHEDULE_MAX_MW:
            errors.append({"field": f"{at}.mw", "message": (
                f"must be a number greater than 0 and at most {_MW_SCHEDULE_MAX_MW}")})
        entries.append((date, mw, i))
    if len(errors) > start:
        return None
    entries.sort(key=lambda e: e[0])
    for pos in range(1, len(entries)):
        date, mw, i = entries[pos]
        clash = next((d for d, _, _ in entries[:pos]
                      if d == date or (7 in (len(d), len(date)) and d[:7] == date[:7])), None)
        if clash is not None:
            errors.append({"field": f"{path}[{i}].date", "message": (
                f"repeats {clash}: each date appears once, and a month covers its days")})
        prev_date, prev_mw, _ = entries[pos - 1]
        if mw < prev_mw:
            errors.append({"field": f"{path}[{i}].mw", "message": (
                f"must be at least {_json_number(prev_mw)}, the MW at {prev_date}: "
                "mw_schedule is cumulative MW available by each date")})
    if len(errors) > start:
        return None
    return [{"date": d, "mw": _json_number(m)} for d, m, _ in entries]


def _check_power(value, path, errors, now):
    start = len(errors)
    keys = ("utility", "substation", "interconnection_stage")
    if not isinstance(value, dict):
        errors.append({"field": path, "message": (
            "must be an object with utility, substation and/or interconnection_stage")})
        return None
    _no_other_keys(value, keys, path, errors, "power")
    out = {}
    for key in keys:
        if value.get(key) is None:
            continue
        if key == "interconnection_stage":
            val, problem = _field_choice(value[key], _INTERCONNECTION_STAGES)
        else:
            val, problem = _field_text(value[key], _FIELD_TEXT_MAX)
        if problem:
            errors.append({"field": f"{path}.{key}", "message": problem})
        else:
            out[key] = val
    if len(errors) == start and not out:
        errors.append({"field": path, "message": (
            "give at least one of utility, substation, interconnection_stage")})
    return out if len(errors) == start else None


def _check_price(value, path, errors, now):
    """{"on_request": true}, or a band {"low", "high", "unit"}."""
    start = len(errors)
    shape = 'must be {"on_request": true} or {"low", "high", "unit"}'
    if not isinstance(value, dict):
        errors.append({"field": path, "message": shape})
        return None
    _no_other_keys(value, ("on_request", "low", "high", "unit"), path, errors, "price")
    band = [k for k in ("low", "high", "unit") if value.get(k) is not None]
    if value.get("on_request") is not None:
        if value["on_request"] is not True:
            errors.append({"field": f"{path}.on_request",
                           "message": "must be true; send low, high and unit for a price band"})
        elif band:
            errors.append({"field": path,
                           "message": "give on_request or a low/high/unit band, not both"})
        return {"on_request": True} if len(errors) == start else None
    if not band:
        if len(errors) == start:
            errors.append({"field": path, "message": shape})
        return None
    low, high = _num(value.get("low")), _num(value.get("high"))
    if low is None or low < 0:
        errors.append({"field": f"{path}.low", "message": "must be a number of at least 0"})
    if high is None:
        errors.append({"field": f"{path}.high", "message": "must be a number of at least low"})
    elif low is not None and low >= 0 and high < low:
        errors.append({"field": f"{path}.high", "message": "must be at least low"})
    unit, problem = _field_choice(value.get("unit"), _PRICE_UNITS)
    if problem:
        errors.append({"field": f"{path}.unit", "message": problem})
    if len(errors) > start:
        return None
    return {"low": _json_number(low), "high": _json_number(high), "unit": unit}


def _check_provider(value, path, errors, now):
    start = len(errors)
    if not isinstance(value, dict):
        errors.append({"field": path, "message": 'must be {"name", "disclosed"}'})
        return None
    _no_other_keys(value, ("name", "disclosed"), path, errors, "provider")
    name, problem = _field_text(value.get("name"), _FIELD_TEXT_MAX)
    if problem:
        errors.append({"field": f"{path}.name", "message": problem})
    disclosed = value.get("disclosed")
    if not isinstance(disclosed, bool):
        errors.append({"field": f"{path}.disclosed", "message": (
            "is required: true or false" if disclosed is None else "must be true or false")})
    if len(errors) > start:
        return None
    return {"name": name, "disclosed": disclosed}


def _check_verification(value, path, errors, now):
    start = len(errors)
    if not isinstance(value, dict):
        errors.append({"field": path,
                       "message": 'must be {"verified_by", "verified_at", "method"}'})
        return None
    _no_other_keys(value, ("verified_by", "verified_at", "method"), path, errors,
                   "verification")
    verified_by, problem = _field_text(value.get("verified_by"), _FIELD_TEXT_MAX)
    if problem:
        errors.append({"field": f"{path}.verified_by", "message": problem})
    verified_at = _parse_verified_at(value.get("verified_at"))
    if verified_at is None:
        errors.append({"field": f"{path}.verified_at", "message": (
            "is required" if value.get("verified_at") is None else
            "must be an ISO-8601 date or date-time (no offset means UTC)")})
    elif verified_at - now > _VERIFIED_AT_MAX_AHEAD:
        errors.append({"field": f"{path}.verified_at",
                       "message": "must not be more than 1 day in the future"})
    method, problem = _field_choice(value.get("method"), _VERIFICATION_METHODS)
    if problem:
        errors.append({"field": f"{path}.method", "message": problem})
    if len(errors) > start:
        return None
    return {"verified_by": verified_by, "verified_at": ledger.iso_utc(verified_at),
            "method": method}


def _check_site(value, path, errors, now):
    """The site itself: name, address, city, postal_code and/or parcel_id. Only
    a released identity block serves it."""
    start = len(errors)
    if not isinstance(value, dict):
        errors.append({"field": path, "message": (
            "must be an object with name, address, city, postal_code and/or parcel_id")})
        return None
    _no_other_keys(value, _SITE_KEYS, path, errors, "site")
    out = {}
    for key in _SITE_KEYS:
        if value.get(key) is None:
            continue
        val, problem = _field_text(value[key], _FIELD_TEXT_MAX)
        if problem:
            errors.append({"field": f"{path}.{key}", "message": problem})
        else:
            out[key] = val
    if len(errors) == start and not out:
        errors.append({"field": path, "message": (
            "give at least one of name, address, city, postal_code, parcel_id")})
    return out if len(errors) == start else None


def _check_update_cadence(value, path, errors, now):
    """How often the provider updates the listing: real_time, weekly or monthly."""
    choice, problem = _field_choice(value, _UPDATE_CADENCES)
    if problem:
        errors.append({"field": path, "message": problem})
    return choice


_DETAIL_FIELD_CHECKS = {
    "colocation": _check_colocation,
    "delivery_type": _check_delivery_type,
    "mw_schedule": _check_mw_schedule,
    "power": _check_power,
    "price": _check_price,
    "provider": _check_provider,
    "site": _check_site,
    "update_cadence": _check_update_cadence,
    "verification": _check_verification,
}


def _reserved_detail_key(key):
    """The reserved field a `detail` key names, ignoring case and surrounding
    whitespace; None for every other key."""
    if not isinstance(key, str):
        return None
    name = key.strip().lower()
    return name if name in _DETAIL_RESERVED_KEYS else None


def _identity_detail_key(key):
    """True for a free-form `detail` key that identifies the site or provider
    (_IDENTITY_DETAIL_KEYS), ignoring case and surrounding whitespace."""
    return isinstance(key, str) and key.strip().lower() in _IDENTITY_DETAIL_KEYS


def _validate_detail(raw):
    """Admin-write rules for `detail`. -> (value to store, errors).

    `raw` is an object, a JSON string holding one, or None (stores NULL). Each
    reserved key is checked and stored normalized: text trimmed, numbers as
    JSON numbers, mw_schedule sorted by date. A reserved key set to null is
    dropped, and a reserved name in other case or spacing is refused, as is
    colocation unless delivery_type is colocation. Every other key is stored
    as sent. Any error means nothing is stored."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, RecursionError):
            return None, [{"field": "detail",
                           "message": "must be a JSON object; the string is not valid JSON"}]
    if raw is None:
        return None, []
    if not isinstance(raw, dict):
        return None, [{"field": "detail", "message": "must be a JSON object"}]
    now, errors, out = _now(), [], {}
    for key, value in raw.items():
        name = _reserved_detail_key(key)
        if name is None:
            out[key] = value
        elif key != name:
            errors.append({"field": f"detail.{key}",
                           "message": f"names a reserved field; spell it {name}"})
        elif value is not None:
            normalized = _DETAIL_FIELD_CHECKS[name](value, "detail." + name, errors, now)
            if normalized is not None:
                out[name] = normalized
    if raw.get("colocation") is not None and not _colocation_allowed(out.get("delivery_type")):
        errors.append({"field": "detail.colocation",
                       "message": "is allowed only when delivery_type is colocation"})
    return (None, errors) if errors else (out, [])


def _invalid(code, errors):
    first = errors[0]
    summary = f"{first['field']} {first['message']}"
    if len(errors) > 1:
        summary += f" (and {len(errors) - 1} more)"
    return _err(400, code, " ".join(summary.split()), errors=errors)


def _invalid_detail(errors):
    return _invalid("invalid_detail", errors)


def _https_url(value, hosts=None):
    """An https URL with a host (one of `hosts` when given), no credentials and
    no whitespace, of at most _URL_MAX characters."""
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text or len(text) > _URL_MAX or any(ch.isspace() for ch in text):
        return False
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower()
        credentials = parts.username or parts.password
    except ValueError:
        return False
    if parts.scheme.lower() != "https" or not host or credentials:
        return False
    return host in hosts if hosts else "." in host


def _calendar_date(value):
    """A trimmed 'YYYY-MM-DD' that names a real calendar date, else None."""
    if not isinstance(value, str):
        return None
    m = _POSTED_AT_RE.fullmatch(value.strip())
    if not m:
        return None
    try:
        datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    return value.strip()


def _validate_contact(raw):
    """Admin-write rules for `contact`. Only its co_marketing record is checked;
    every other key is stored as sent. -> errors (nothing is stored on any)."""
    contact = raw
    if isinstance(raw, str):
        try:
            contact = json.loads(raw)
        except (ValueError, RecursionError):
            return []
    if not isinstance(contact, dict) or contact.get("co_marketing") is None:
        return []
    value, path, errors = contact["co_marketing"], "contact.co_marketing", []
    if not isinstance(value, dict):
        return [{"field": path, "message": (
            "must be an object with linkedin_post_url, posted_at and/or website_url")}]
    _no_other_keys(value, _CO_MARKETING_KEYS, path, errors, "co_marketing")
    if (value.get("linkedin_post_url") is not None
            and not _https_url(value["linkedin_post_url"], _LINKEDIN_HOSTS)):
        errors.append({"field": f"{path}.linkedin_post_url",
                       "message": "must be an https URL on linkedin.com or www.linkedin.com"})
    if value.get("posted_at") is not None and _calendar_date(value["posted_at"]) is None:
        errors.append({"field": f"{path}.posted_at",
                       "message": "must be a calendar date as YYYY-MM-DD"})
    if value.get("website_url") is not None and not _https_url(value["website_url"]):
        errors.append({"field": f"{path}.website_url", "message": "must be an https URL"})
    return errors


def _freshness(verification, now, cadence=None):
    """fresh / aging / stale by whole days since verification.verified_at;
    unverified without a valid verification. With an update_cadence and a
    verification, next_update_due is verified_at plus that cadence's allowance
    (_CADENCE_OVERDUE_DAYS) and overdue is whether now is past it. A badge
    only: an overdue listing is still served."""
    out = {"state": "unverified", "verified_at": None, "age_days": None,
           "update_cadence": cadence, "next_update_due": None, "overdue": False}
    if not verification:
        return out
    verified = _parse_verified_at(verification["verified_at"])
    age_days = max(0, (now - verified).days)
    if age_days <= _FRESH_MAX_AGE_DAYS:
        state = "fresh"
    elif age_days <= _AGING_MAX_AGE_DAYS:
        state = "aging"
    else:
        state = "stale"
    out.update(state=state, verified_at=verification["verified_at"], age_days=age_days)
    if cadence in _CADENCE_OVERDUE_DAYS:
        due = verified + timedelta(days=_CADENCE_OVERDUE_DAYS[cadence])
        out.update(next_update_due=ledger.iso_utc(due), overdue=now > due)
    return out


def _listing_fields(detail):
    """The reserved fields of a stored `detail`, each checked by the write
    rules as of now, plus `freshness`. A field that fails them (stored before
    the rules applied) reads as None."""
    now = _now()
    fields = {}
    for name, check in _DETAIL_FIELD_CHECKS.items():
        value = detail.get(name)
        fields[name] = None if value is None else check(value, "detail." + name, [], now)
    if not _colocation_allowed(fields["delivery_type"]):
        fields["colocation"] = None
    fields["freshness"] = _freshness(fields["verification"], now, fields["update_cadence"])
    return fields


def _terms_ok(v):
    """Has this viewer accepted the CURRENT introduction terms? Fails CLOSED:
    no identity, or a lookup that fails, reads as not accepted."""
    if not (v["identified"] and v["user_ref"]):
        return False
    try:
        return bool(_db_terms_accepted(v["user_ref"], TERMS_VERSION))
    except Exception as exc:
        logger.warning("[pocket-listings] terms lookup failed: %s", exc)
        return False


def _access(row, v, return_path, terms_ok=False):
    """terms_ok defaults to False, so a caller that does not pass it keeps the
    listing locked rather than opening it."""
    required = row.get("tier_required") if row.get("tier_required") in _ACCESS_LEVELS else "registered"
    if row.get("status") == "public":
        granted, reason = True, None
    elif not v["identified"]:
        granted, reason = False, (v["reason"] or "sign_in_required")
    elif _TIER_RANK.get(v["tier"], 0) < _REQUIRED_RANK.get(required, 1):
        granted, reason = False, "upgrade_required"
    elif not terms_ok:
        granted, reason = False, "terms_acceptance_required"
    else:
        granted, reason = True, None
    unlock = None
    if reason == "terms_acceptance_required":
        unlock = {"web_sign_in_url": None,
                  "mcp_steps": ["accept_capacity_terms"],
                  "pricing_url": None,
                  "terms": _terms_block(),
                  "accept": {"method": "POST", "path": "/api/v1/listings/terms/accept"}}
    elif not granted:
        steps = {"sign_in_required": ["claim_free_key", "bind_email"],
                 "email_binding_required": ["bind_email"],
                 "upgrade_required": ["unlock_more_data"]}[reason]
        unlock = {"web_sign_in_url": (None if reason == "upgrade_required"
                                      else _sign_in_url(return_path)),
                  "mcp_steps": steps,
                  "pricing_url": PRICING_URL if reason == "upgrade_required" else None}
    return {"required": required, "granted": granted, "reason": reason, "unlock": unlock}


def _capacity_kw(row, fields):
    """kW on offer, as _size_sql sizes a listing: a colocation listing's
    colocation.kw_available (None without one), any other listing's
    capacity_mw * 1000, else None."""
    if fields["delivery_type"] == "colocation":
        return (fields["colocation"] or {}).get("kw_available")
    mw = _num(row.get("capacity_mw"))
    return None if mw is None else _json_number(round(mw * 1000, 3))


def _teaser(row, access, fields=None):
    """The card everyone sees, locked or not. Never any site identity.
    `fields` is _listing_fields(detail), when the caller already has it."""
    detail = _json_obj(row.get("detail"))
    if fields is None:
        fields = _listing_fields(detail)
    provider = fields["provider"]
    return {
        "id": row.get("id"),
        "slug": row.get("slug"),
        "title": row.get("title"),
        "summary": row.get("summary"),
        "status": row.get("status"),
        "access_required": access["required"],
        "locked": not access["granted"],
        "lock_reason": access["reason"],
        "market": row.get("market"),
        "state": row.get("state"),
        "country": row.get("country"),
        "region": _region_of(row.get("country")),
        "capacity_mw": _num(row.get("capacity_mw")),
        "capacity_kw": _capacity_kw(row, fields),
        "available": _available(detail, fields["mw_schedule"]),
        "delivery_type": fields["delivery_type"],
        "update_cadence": fields["update_cadence"],
        "freshness": fields["freshness"],
        "provider": ({"name": provider["name"]}
                     if provider and provider["disclosed"] else None),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
        "expires_at": _iso(row.get("expires_at")),
        "url": _listing_url(row.get("slug")),
    }


def _full(row, access):
    """The specs view: what an open listing shows until the viewer's own
    registration on it is accepted. Never `contact` or `owner_id`, and no site
    identity: latitude and longitude read null, power carries no substation,
    and the generic `detail` drops reserved keys and the address-like keys of
    _IDENTITY_DETAIL_KEYS. The provider's name appears only when it is
    disclosed. get_listing's `disclosure` block carries the rest once released."""
    detail = _json_obj(row.get("detail"))
    fields = _listing_fields(detail)
    item = _teaser(row, access, fields)
    provider = fields["provider"]
    power = {k: val for k, val in (fields["power"] or {}).items() if k != "substation"}
    item.update({
        "latitude": None,
        "longitude": None,
        "asking_price": _num(row.get("asking_price")),
        "asking_currency": row.get("asking_currency"),
        "detail": {k: val for k, val in detail.items()
                   if isinstance(k, str) and not k.startswith("_")
                   and k.strip().lower() not in _DETAIL_PRIVATE_KEYS
                   and _reserved_detail_key(k) is None
                   and not _identity_detail_key(k)},
        "colocation": fields["colocation"],
        "mw_schedule": fields["mw_schedule"],
        "power": power or None,
        "price": fields["price"],
        "provider": ({"name": provider["name"] if provider["disclosed"] else None,
                      "disclosed": provider["disclosed"]} if provider else None),
        "verification": fields["verification"],
    })
    return item


# ═════════════════════════════════════════════════════════════════════════
#  disclosure (released only by an accepted registration)
# ═════════════════════════════════════════════════════════════════════════

def _identity_text(value):
    """A stored identity value as single-line text of at most _FIELD_TEXT_MAX
    characters, or None."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    return " ".join(str(value).split())[:_FIELD_TEXT_MAX] or None


def _site_identity(detail, site):
    """The reserved `site`, completed from the legacy identity keys for the
    fields it lacks. None when there is nothing to share."""
    out = dict(site or {})
    stored = {}
    for key, value in detail.items():
        if _identity_detail_key(key):
            stored.setdefault(key.strip().lower(), value)
    for legacy, field in _IDENTITY_DETAIL_KEYS.items():
        text = None if field in out else _identity_text(stored.get(legacy))
        if text:
            out[field] = text
    return {key: out[key] for key in _SITE_KEYS if key in out} or None


def _contact_address(value):
    text = str(value or "").strip()
    return text if "@" in text and not any(ch.isspace() for ch in text) else None


def _provider_contact(raw):
    """The provider's contact, for a buyer whose registration it accepted:
    name, email, phone and title from the listing's `contact`. notify_email
    stands in for email only when it is the only address; auto_notify,
    co_marketing and every other key are never read."""
    contact = _json_obj(raw)
    out = {"name": _identity_text(contact.get("name")),
           "email": (_contact_address(contact.get("email"))
                     or _contact_address(contact.get("notify_email"))),
           "phone": _identity_text(contact.get("phone")),
           "title": _identity_text(contact.get("title"))}
    return out if any(out.values()) else None


def _disclosed_values(row):
    """What an accepted registration shares with the buyer about the listing."""
    detail = _json_obj(row.get("detail"))
    fields = _listing_fields(detail)
    provider, power = fields["provider"], fields["power"] or {}
    lat, lon = _num(row.get("latitude")), _num(row.get("longitude"))
    return {"provider": {"name": provider["name"]} if provider else None,
            "site": _site_identity(detail, fields["site"]),
            "latitude": None if lat is None else round(lat, 6),
            "longitude": None if lon is None else round(lon, 6),
            "substation": power.get("substation"),
            "contact": _provider_contact(row.get("contact"))}


def _viewer_lead(rows):
    """(lead_id, status, events) of the latest registration in `rows` — one
    viewer's registrations for one listing — that is not withdrawn, else
    (None, None, [])."""
    by_lead = {}
    for ev in sorted(rows, key=lambda r: r.get("seq") or 0):
        if ev.get("lead_id"):
            by_lead.setdefault(ev["lead_id"], []).append(ev)
    latest = None
    for lead_id, events in by_lead.items():
        opening = _opening(events)
        if not opening or opening.get("event") != "intro_requested":
            continue
        status = ledger.lead_status(events)
        if status == "withdrawn":
            continue
        if latest is None or (opening.get("seq") or 0) > latest[3]:
            latest = (lead_id, status, events, opening.get("seq") or 0)
    return latest[:3] if latest else (None, None, [])


def _disclosure(row, v, access):
    """get_listing's `disclosure` block. Its values are present only when the
    viewer's own registration on this listing is accepted or introduced. A
    locked or unidentified caller gets {"released": false} and nothing more,
    and so does a lookup that fails."""
    locked = {"released": False}
    if not (access["granted"] and v["identified"] and v["user_ref"]):
        return locked
    try:
        rows = _db_viewer_lead_events(v["user_ref"], row.get("id"))
    except Exception as exc:
        logger.warning("[pocket-listings] disclosure lookup failed: %s", exc)
        return locked
    lead_id, lead_status, events = _viewer_lead(rows)
    status = _DISCLOSURE_STATUS.get(lead_status, "none")
    out = {"released": lead_status in _RELEASED_STATUSES, "status": status,
           "lead_id": lead_id, "accepted_at": None, "provider": None, "site": None,
           "latitude": None, "longitude": None, "substation": None, "contact": None}
    if status == "none":
        out["how"] = {"method": "POST", "path": f"/api/v1/listings/{row.get('slug')}/intro",
                      "mcp_tool": "request_capacity_intro"}
    if out["released"]:
        accepted = _first(events, "registration_accepted")
        out["accepted_at"] = _entry(accepted).get("created_at") if accepted else None
        out.update(_disclosed_values(row))
    return out


# ═════════════════════════════════════════════════════════════════════════
#  live availability summary
# ═════════════════════════════════════════════════════════════════════════
# GET /api/v1/listings/summary and the llms.txt Capacity Source block read one
# in-process copy, rebuilt at most once per _SUMMARY_TTL_S. It aggregates the
# teaser-level columns _db_live_listing_facts selects and nothing else.

_SUMMARY_TTL_S = 60
_SUMMARY_MAX_MARKETS = 50
_SUMMARY_LOCK = threading.Lock()
_SUMMARY_CACHE = {"at": None, "value": None}


def _summary_label(value):
    """Stored market / state / country text, whitespace collapsed, or None."""
    text = " ".join(value.split()) if isinstance(value, str) else ""
    return text or None


def _summary_mw(value):
    return None if value is None else _json_number(round(value, 2))


def _build_summary(rows, now):
    """Aggregate live listing rows, newest first. Markets group
    case-insensitively under the newest listing's spelling; a delivery type
    counts only when the listing-field rules accept it."""
    groups, delivery_types = {}, {}
    total_mw, latest = None, None
    for row in rows:
        place = tuple(_summary_label(row.get(k)) for k in ("market", "state", "country"))
        key = tuple(p.casefold() if p else None for p in place)
        group = groups.get(key)
        if group is None:
            group = groups[key] = {"market": place[0], "state": place[1], "country": place[2],
                                   "count": 0, "mw": None, "delivery_types": set()}
        group["count"] += 1
        mw = _num(row.get("capacity_mw"))
        if mw is not None:
            group["mw"] = mw if group["mw"] is None else group["mw"] + mw
            total_mw = mw if total_mw is None else total_mw + mw
        raw_type = row.get("delivery_type")
        if raw_type is not None:
            delivery_type = _check_delivery_type(raw_type, "detail.delivery_type", [], now)
            if delivery_type:
                group["delivery_types"].add(delivery_type)
                delivery_types[delivery_type] = delivery_types.get(delivery_type, 0) + 1
        updated = row.get("updated_at")
        if isinstance(updated, datetime) and (latest is None or updated > latest):
            latest = updated
    ranked = sorted(groups.values(), key=lambda g: (
        -g["count"], g["mw"] is None, -(g["mw"] or 0.0),
        tuple((p or "").casefold() for p in (g["market"], g["state"], g["country"]))))
    return {
        "program_status": _program(len(rows))["status"],
        "live_count": len(rows),
        "total_mw": _summary_mw(total_mw),
        "markets": [dict(g, mw=_summary_mw(g["mw"]), delivery_types=sorted(g["delivery_types"]))
                    for g in ranked[:_SUMMARY_MAX_MARKETS]],
        "market_count": len(groups),
        "delivery_types": delivery_types,
        "latest_updated_at": _iso(latest),
        "generated_at": ledger.iso_utc(now),
    }


def cached_listings_summary():
    """The live availability summary, or None when the listings cannot be
    read. GET /api/v1/listings/summary and llms.txt share it; it is rebuilt at
    most once per _SUMMARY_TTL_S per process, a failed read included."""
    with _SUMMARY_LOCK:
        at = _SUMMARY_CACHE["at"]
        if at is not None and time.monotonic() - at < _SUMMARY_TTL_S:
            return _SUMMARY_CACHE["value"]
        value = None
        if _dsn():
            try:
                _ensure_schema()
                value = _build_summary(_db_live_listing_facts(), datetime.now(timezone.utc))
            except Exception as exc:
                logger.warning("[pocket-listings] summary failed: %s", exc)
        _SUMMARY_CACHE.update(at=time.monotonic(), value=value)
        return value


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
    kw = raw_req.get("capacity_kw")
    if kw not in (None, ""):
        n = _num(kw)
        if n is None or not (0 < n <= _REQUIREMENT_MAX_KW):
            problems["requirement.capacity_kw"] = (
                f"must be a number of kW greater than 0 and at most {_REQUIREMENT_MAX_KW}")
        else:
            requirement["capacity_kw"] = _json_number(round(n, 3))
    for key, max_items, limit in (("markets", 10, 60), ("states", 15, 40)):
        vals = _string_list(raw_req.get(key), max_items, limit)
        if vals is False:
            problems[f"requirement.{key}"] = "must be a list of names"
        elif vals:
            requirement[key] = vals
    regions = _string_list(raw_req.get("regions"), _SEARCH_MAX_VALUES, 60)
    if regions is False:
        problems["requirement.regions"] = "must be a list of regions"
    elif regions:
        keys, unknown = _region_keys(regions)
        if unknown:
            problems["requirement.regions"] = "must be regions: " + ", ".join(REGION_KEYS)
        else:
            requirement["regions"] = keys
    countries = _string_list(raw_req.get("countries"), _SEARCH_MAX_VALUES + 1, 60)
    if countries is False:
        problems["requirement.countries"] = "must be a list of ISO country codes or names"
    elif len(countries) > _SEARCH_MAX_VALUES:
        problems["requirement.countries"] = f"must list at most {_SEARCH_MAX_VALUES} countries"
    elif countries:
        requirement["countries"] = _country_values(countries)[2]
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
    if need_requirement and not any(k in requirement for k in (
            "capacity_mw", "capacity_kw", "markets", "states", "regions", "countries")):
        problems.setdefault("requirement", "give at least one of capacity_mw, capacity_kw, "
                                           "markets, states, regions or countries")

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
    labels = (("capacity_mw", "Capacity (MW)"), ("capacity_kw", "Capacity (kW)"),
              ("markets", "Markets"), ("states", "States"), ("regions", "Regions"),
              ("countries", "Countries"), ("timeline", "Timeline"),
              ("use_case", "Use case"), ("notes", "Notes"))
    for key, label in labels:
        val = req.get(key)
        if val in (None, "", []):
            continue
        if key == "regions" and isinstance(val, list):
            val = [_REGION_LABELS.get(x, x) for x in val]
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
        after = ("Once you confirm, DC Hub records the request in its lead register and sends "
                 "the operator your company name and requirement so it can accept or decline "
                 "the registration. If it accepts, DC Hub shares your name, role, email and "
                 "message with the operator, and the site details and the operator's contact "
                 "with you. If it declines, neither side's contact details are shared.")
    else:
        what = "first access to Capacity Source listings matching your requirement"
        subject = "Confirm your DC Hub Capacity Source requirement"
        after = ("Once you confirm, DC Hub records your requirement in its lead register and "
                 "emails you when a matching listing opens.")
    body = (
        f"<p>Hi {_e(name, 'there')},</p>"
        f"<p>You asked DC Hub for {what}. Confirm the request so DC Hub can register it:</p>"
        f"<p><a href=\"{html.escape(url, quote=True)}\" style=\"display:inline-block;"
        f"padding:10px 18px;background:#6366f1;color:#ffffff;border-radius:6px;"
        f"text-decoration:none\">Confirm my request</a></p>"
        f"<p>Lead reference: <b>{_e(lead_id)}</b></p>"
        f"{_requirement_html(requirement)}"
        f"<p>{_e(after)}</p>"
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
    # The subject keeps "Pocket listing lead" after the Capacity Source rename on
    # purpose: the admin inbox may filter on it (dchub-frontend #1473 kept the
    # /listings mailto subjects for the same reason).
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
        f"/api/v1/admin/listings/leads/{_e(lead_id)}/notify-operator · record the operator's "
        f"decision: POST /api/v1/admin/listings/leads/{_e(lead_id)}/status "
        f"{{\"status\":\"accepted\"}} or {{\"status\":\"declined\"}} · once accepted, mark "
        f"introduced: {{\"status\":\"introduced\"}}</p>"
    )
    return subject, body


def _scrub_phone(match):
    text = match.group(0)
    digits = sum(ch.isdigit() for ch in text)
    if digits > 15:
        return text
    if ((text[0] in "+(" and digits >= 7) or digits >= 10
            or _SCRUB_LOCAL_PHONE_RE.search(text)):
        return "[phone removed]"
    return text


def _scrub_text(text):
    """Free text with email addresses, links and phone numbers taken out.
    Dates and year ranges (2027-06-15, 2026-2028) stay."""
    if not isinstance(text, str):
        return text
    text = _SCRUB_EMAIL_RE.sub("[email removed]", text)
    text = _SCRUB_URL_RE.sub("[link removed]", text)
    return _SCRUB_PHONE_RE.sub(_scrub_phone, text)


def _scrub_requirement(req):
    """A requirement as the provider sees it before accepting: the free-text
    fields (_SCRUB_TEXT_FIELDS) scrubbed, the rest as registered."""
    if not isinstance(req, dict):
        return req
    return {k: (_scrub_text(val) if k in _SCRUB_TEXT_FIELDS else val) for k, val in req.items()}


def _operator_lead_view(lead_id, events):
    """What the provider sees of one registration, in its notice and its
    ledger alike: lead id, status, registered and confirmed times, company,
    requirement, arrival channel and platform, register entries and the verify
    URL. The buyer's name, role, email, email domain and message are added only
    once the registration is accepted (_RELEASED_STATUSES); until then the
    requirement's free text is scrubbed as well."""
    opening, entry, confirmed = _lead_facts(events)
    status = ledger.lead_status(events)
    released = status in _RELEASED_STATUSES
    requirement = _requirement_of(opening)
    return {
        "lead_id": lead_id,
        "status": status,
        "registered_at": entry.get("created_at"),
        "confirmed_at": _entry(confirmed).get("created_at") if confirmed else None,
        "company": opening.get("company"),
        "requirement": requirement if released else _scrub_requirement(requirement),
        "channel": entry.get("channel"),
        "platform": entry.get("platform"),
        "email_verified": bool(entry.get("email_verified") or confirmed),
        "verified_via": entry.get("verified_via") or ("email_link" if confirmed else None),
        "name": opening.get("name") if released else None,
        "role": opening.get("role") if released else None,
        "email": opening.get("email") if released else None,
        "email_domain": entry.get("email_domain") if released else None,
        "message": opening.get("message") if released else None,
        "entries": [{"seq": ev.get("seq"), "event": ev.get("event"),
                     "at": _entry(ev).get("created_at"), "entry_hash": ev.get("entry_hash")}
                    for ev in events],
        "verify_url": _verify_url(lead_id),
    }


def _operator_link(secret, row):
    """(token, url) of the provider's lead page for this listing, with a fresh
    operator token, in the form admin_operator_link hands out. Opening the page
    changes nothing: a decision is a POST from it."""
    token = ledger.operator_token(secret, row["id"])
    return token, (f"{SITE}/listings?ledger={quote(str(row.get('slug')), safe='')}"
                   f"&token={quote(token, safe='')}")


def _operator_email(lead_id, events, listing_row, secret=None):
    view = _operator_lead_view(lead_id, events)
    status = view["status"]
    title = listing_row.get("title") or listing_row.get("slug")
    subject = _hdr(f"Lead registration notice {lead_id} — {title}")
    entries = "".join(
        f"<tr><td style='padding:2px 12px 2px 0'>#{_e(ev['seq'])}</td>"
        f"<td style='padding:2px 12px 2px 0'>{_e(ev['event'])}</td>"
        f"<td style='padding:2px 12px 2px 0'>{_e(ev['at'])}</td>"
        f"<td style='font-family:monospace;font-size:12px'>{_e(ev['entry_hash'])}</td></tr>"
        for ev in view["entries"])
    via = ("an AI agent" + (f" ({view['platform']})" if view["platform"] else "")
           if view["channel"] == "mcp" else "dchub.cloud")
    prospect = f"<b>Company:</b> {_e(view['company'])}<br>"
    if status in _RELEASED_STATUSES:
        prospect = (f"<b>Name:</b> {_e(view['name'])}<br><b>Role:</b> {_e(view['role'])}<br>"
                    f"{prospect}<b>Email:</b> {_e(view['email'])}<br>"
                    f"<b>Message:</b> {_e(view['message']).replace(chr(10), '<br>')}<br>")
    link = (_operator_link(secret, listing_row)[1]
            if secret is not None and listing_row.get("id") is not None else None)
    page = (f"<a href=\"{html.escape(link, quote=True)}\">your lead page</a>" if link
            else f"a reply to {_e(SUPPORT_EMAIL)}")
    if status in _DECIDABLE_STATUSES:
        decision = (f"<p><b>Accept or decline this registration</b> on {page}. If you accept, "
                    f"DC Hub shares the prospect's name, role, email and message with you, and "
                    f"your site details and contact with the prospect. If you decline, neither "
                    f"side's contact details are shared.</p>")
    elif status in _RELEASED_STATUSES:
        decision = f"<p>You accepted this registration. Your lead page: {page}.</p>"
    elif status == "declined":
        decision = "<p>This registration was declined. No contact details were shared.</p>"
    else:
        decision = ""
    body = (
        f"<p>DC Hub has registered the following prospect for your listing "
        f"<b>{_e(title)}</b>.</p>"
        f"<p><b>Lead:</b> {_e(lead_id)}<br>"
        f"<b>Status:</b> {_e(status)}<br>"
        f"{prospect}"
        f"<b>Arrived via:</b> {_e(via)}<br>"
        f"<b>Registered (UTC):</b> {_e(view['registered_at'])}<br>"
        f"<b>Inbox confirmed (UTC):</b> {_e(view['confirmed_at'], 'verified at request')}</p>"
        f"{_requirement_html(view['requirement'])}"
        f"{decision}"
        f"<p><b>Register entries for this lead</b></p><table>{entries}</table>"
        f"<p>Check the live record at any time: {_e(view['verify_url'])}. Keep "
        f"this email: its receipt time and the hashes above are your own record "
        f"that DC Hub registered this prospect for your listing.</p>"
        f"<p>Reply to {_e(SUPPORT_EMAIL)} about this lead.</p>"
    )
    return subject, body


def _operator_address(contact):
    for key in ("notify_email", "email"):
        val = str(contact.get(key) or "").strip()
        if "@" in val:
            return val
    return None


# ── decision emails ──────────────────────────────────────────────────────

def _h(value):
    """Escaped text, or None for an empty value (a row _facts_html leaves out)."""
    return None if value in (None, "") else _e(value)


def _facts_html(pairs):
    rows = "".join(f"<tr><td style='padding:2px 12px 2px 0;color:#555'>{_e(label)}</td>"
                   f"<td>{value}</td></tr>" for label, value in pairs if value)
    return f"<table>{rows}</table>" if rows else ""


def _mailto(address):
    if not address:
        return None
    href = html.escape("mailto:" + quote(address, safe="@.+-_"), quote=True)
    return f"<a href=\"{href}\">{_e(address)}</a>"


def _accepted_buyer_email(lead_id, listing_row, opening, note):
    """To the buyer: the provider accepted. The provider's name, the site, its
    map location and substation, the provider's contact, and the listing link."""
    values = _disclosed_values(listing_row)
    title = listing_row.get("title") or listing_row.get("slug")
    site, contact = values["site"] or {}, values["contact"] or {}
    lat, lon = values["latitude"], values["longitude"]
    location = None
    if lat is not None and lon is not None:
        maps = f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
        location = f"{_e(lat)}, {_e(lon)} (<a href=\"{html.escape(maps, quote=True)}\">map</a>)"
    contact_html = _facts_html([
        ("Name", _h(contact.get("name"))), ("Title", _h(contact.get("title"))),
        ("Email", _mailto(contact.get("email"))), ("Phone", _h(contact.get("phone")))])
    subject = _hdr(f"Registration accepted — {title}")
    body = (
        f"<p>Hi {_e(opening.get('name'), 'there')},</p>"
        f"<p>The provider accepted your registration <b>{_e(lead_id)}</b> for "
        f"<b>{_e(title)}</b>. Here are the site details and the provider's contact.</p>"
        + _facts_html([
            ("Provider", _h((values["provider"] or {}).get("name"))),
            ("Site", _h(site.get("name"))), ("Address", _h(site.get("address"))),
            ("City", _h(site.get("city"))), ("Postal code", _h(site.get("postal_code"))),
            ("Parcel", _h(site.get("parcel_id"))), ("Map location", location),
            ("Substation", _h(values["substation"]))])
        + "<p><b>Provider contact</b></p>"
        + (contact_html or f"<p>No provider contact is on file for this listing. Reply to "
                           f"{_e(SUPPORT_EMAIL)} and DC Hub will put you in touch.</p>")
        + (f"<p><b>Note from the provider:</b><br>{_e(note).replace(chr(10), '<br>')}</p>"
           if note else "")
        + f"<p><a href=\"{html.escape(_listing_url(listing_row.get('slug')), quote=True)}\">"
          f"Open the listing on DC Hub</a></p>"
        + f"<p style='color:#555;font-size:13px'>Registration record: {_e(_verify_url(lead_id))}</p>"
    )
    return subject, body


def _accepted_provider_email(lead_id, listing_row, opening):
    """To the provider's contact address: the buyer's name, role, company,
    email, message and requirement."""
    title = listing_row.get("title") or listing_row.get("slug")
    message = opening.get("message")
    subject = _hdr(f"Registration accepted {lead_id} — {title}: buyer contact")
    body = (
        f"<p>You accepted registration <b>{_e(lead_id)}</b> for <b>{_e(title)}</b>. DC Hub "
        f"has sent the buyer your site details and contact. The buyer:</p>"
        + _facts_html([
            ("Name", _h(opening.get("name"))), ("Role", _h(opening.get("role"))),
            ("Company", _h(opening.get("company"))), ("Email", _mailto(opening.get("email"))),
            ("Message", _h(message).replace(chr(10), "<br>") if message else None)])
        + _requirement_html(_requirement_of(opening))
        + f"<p style='color:#555;font-size:13px'>Registration record: {_e(_verify_url(lead_id))}</p>"
    )
    return subject, body


def _declined_buyer_email(lead_id, listing_row, opening):
    """To the buyer: the provider could not take the registration. No provider
    identity, no contact."""
    title = listing_row.get("title") or listing_row.get("slug")
    subject = _hdr(f"Update on your registration — {title}")
    body = (
        f"<p>Hi {_e(opening.get('name'), 'there')},</p>"
        f"<p>The provider could not take your registration <b>{_e(lead_id)}</b> for "
        f"<b>{_e(title)}</b>. No contact details were shared with either side.</p>"
        f"<p><a href=\"{SITE}/listings#listings\">Browse the other listings</a>, or "
        f"<a href=\"{SITE}/listings#register\">register your requirement</a> and DC Hub "
        f"will email you when matching capacity opens.</p>"
    )
    return subject, body


def _decision_admin_email(lead_id, listing_row, decision, channel, note, copies):
    verb = _DECISION_STATUS[decision]
    title = listing_row.get("title") or listing_row.get("slug")
    # "Pocket listing lead" keeps the admin inbox's filter working (see _admin_email).
    subject = _hdr(f"Pocket listing lead {lead_id} {verb} — {title}")
    sent = "".join(
        f"<hr><p><b>{_e(label)}</b> — to {_e(to, 'no address on file, not sent')}<br>"
        f"<b>Subject:</b> {_e(copy_subject)}</p>{copy_body}"
        for label, to, copy_subject, copy_body in copies)
    body = (
        f"<h2>Registration {_e(verb)}: {_e(lead_id)}</h2>"
        f"<p><b>Listing:</b> {_e(title)} {_e(listing_row.get('slug'), '')}<br>"
        f"<b>Recorded via:</b> {_e(channel)}<br>"
        f"<b>Note:</b> {_e(note)}</p>{sent}"
    )
    return subject, body


def _on_decision(decision, lead_id, listing_row, opening, channel, note):
    """Off-request: the emails a recorded decision sends. Accept: the buyer
    gets the site and the provider's contact, the provider the buyer's details.
    Decline: the buyer hears the registration was not taken, and nothing about
    the provider. The admin inbox gets a copy of each. Never blocks the
    response; a failure is logged."""
    def work():
        try:
            buyer = opening.get("email")
            if decision == "accept":
                copies = [("To the buyer", buyer,
                           *_accepted_buyer_email(lead_id, listing_row, opening, note)),
                          ("To the provider", _operator_address(_json_obj(listing_row.get("contact"))),
                           *_accepted_provider_email(lead_id, listing_row, opening))]
            else:
                copies = [("To the buyer", buyer,
                           *_declined_buyer_email(lead_id, listing_row, opening))]
            for label, to, subject, body in copies:
                if to:
                    _send_email(to, subject, body)
                else:
                    logger.warning("[pocket-listings] %s: no address for %s", lead_id, label.lower())
            subject, body = _decision_admin_email(lead_id, listing_row, decision, channel, note,
                                                  copies)
            _send_email(_admin_inbox(), subject, body)
        except Exception as exc:  # noqa: BLE001 — a mail failure never reaches the response
            logger.warning("[pocket-listings] decision emails for %s failed: %s", lead_id, exc)
    _dispatch(work)


def _notify_operator(lead_id, listing_row, secret, events, to=None):
    contact = _json_obj(listing_row.get("contact"))
    address = to or _operator_address(contact)
    if not address:
        return {"sent": False, "reason": "no_operator_address", "ledger": None}
    subject, body = _operator_email(lead_id, events, listing_row, secret)
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
        # The request itself carries the terms acceptance, validated below.
        acc = _access(row, v, return_path, terms_ok=True)
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
        elif row is not None:
            nxt = (f"A confirmation link was sent to {masked}. Once it is confirmed, DC Hub "
                   f"sends the provider your company name and requirement to accept or decline.")
        else:
            nxt = (f"A confirmation link was sent to {masked}. The requirement is registered "
                   f"once it is confirmed.")
    elif status == "withdrawn":
        nxt = f"This request was withdrawn. Email {SUPPORT_EMAIL} to reopen it."
    elif status == "declined":
        nxt = ("The provider could not take this registration. Browse the other listings or "
               "register your requirement.")
    elif status in _RELEASED_STATUSES:
        nxt = ("The provider accepted this registration. Open the listing for the site details "
               "and the provider's contact.")
    elif row is not None:
        nxt = ("Registered. DC Hub sends the provider your company name and requirement; if it "
               "accepts, you get the site details and its contact by email.")
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

def _search_values(name):
    """A comma-list query parameter, given once or repeated -> (values with
    whitespace collapsed, first spelling of each kept; problem or None)."""
    values, given = [], 0
    for raw in request.args.getlist(name):
        for part in raw.split(","):
            text = " ".join(part.split())
            if not text:
                continue
            given += 1
            if len(text) > _SEARCH_VALUE_MAX:
                return None, f"each {name} value must be at most {_SEARCH_VALUE_MAX} characters"
            if text.lower() not in (x.lower() for x in values):
                values.append(text)
    if given > _SEARCH_MAX_VALUES:
        return None, f"{name} takes at most {_SEARCH_MAX_VALUES} values"
    return values, None


@exclusive_listings_bp.route("/api/v1/listings", methods=["GET"])
def list_listings():
    """Teaser feed. Everyone sees every live listing's teaser; nobody sees
    operator contact or site identity.

    Size — give one of:
    ?min_kw=         kW on offer at least this: a colocation listing's
                     colocation.kw_available (a colocation listing without one
                     does not match), any other listing's capacity_mw * 1000.
                     A number greater than 0, else 400 invalid_request
    ?min_mw=         capacity_mw at least this. With min_kw, 400 invalid_request
    Location — comma lists, repeatable, at most 20 values of 80 characters:
    ?region=         north_america | latin_america | europe | asia_pacific |
                     middle_east_africa, or an alias: na, north america,
                     latam, latin america, south america, emea (europe +
                     middle_east_africa), apac, americas (north_america +
                     latin_america), mea, middle east, africa. Case, spaces
                     and hyphens do not matter; an unknown value is 400
                     invalid_request with `allowed`. Countries are placed by
                     canonical_stats' map
    ?country=        ISO-2 codes or country names
    ?location=       free text; a listing matches when ANY term is a region
                     name or alias, a country code or name, a US state code or
                     name, or part of its market (3+ characters)
    ?market=         market name, case-insensitive
    ?state=          state code
    Other:
    ?delivery_type=  land | powered_shell | turnkey | colocation, matched
                     exactly; any other value is 400 invalid_request with
                     `allowed`
    ?available_by=   YYYY-MM or YYYY-MM-DD: listings with at least one
                     mw_schedule entry dated in or before that month (listings
                     without a schedule do not match); a malformed value is
                     400 invalid_request
    ?limit=          default 50, max 200

    The filter families AND together, and every filter is in the SQL WHERE
    clause, so LIMIT counts matching rows only. `filters` echoes the
    normalised filters applied, e.g. {"min_kw": 500, "regions":
    ["north_america", "europe"], "location": ["Dallas"]}. Every teaser carries
    capacity_kw and region."""
    _ensure_schema()
    v = _viewer()
    market = (request.args.get("market") or "").strip()[:80]
    state = (request.args.get("state") or "").strip().upper()[:40]
    raw_min_mw = (request.args.get("min_mw") or "").strip()
    raw_min_kw = (request.args.get("min_kw") or "").strip()
    if raw_min_mw and raw_min_kw:
        return _err(400, "invalid_request", "give min_mw or min_kw, not both")
    min_mw = _num(raw_min_mw) if raw_min_mw else None
    min_kw = None
    if raw_min_kw:
        min_kw = _num(raw_min_kw)
        if min_kw is None or min_kw <= 0:
            return _err(400, "invalid_request", "min_kw must be a number greater than 0")
    delivery_type = (request.args.get("delivery_type") or "").strip()
    if delivery_type and delivery_type not in _DELIVERY_TYPES:
        return _err(400, "invalid_request", "invalid delivery_type",
                    allowed=list(_DELIVERY_TYPES))
    available_by = (request.args.get("available_by") or "").strip()
    if available_by and _schedule_date(available_by) is None:
        return _err(400, "invalid_request",
                    "available_by must be a calendar date as YYYY-MM or YYYY-MM-DD")
    searched = {}
    for name in ("region", "country", "location"):
        values, problem = _search_values(name)
        if problem:
            return _err(400, "invalid_request", problem)
        searched[name] = values
    regions, unknown = _region_keys(searched["region"])
    if unknown:
        return _err(400, "invalid_request", "unknown region: " + ", ".join(unknown),
                    allowed=list(REGION_KEYS))
    countries, location = searched["country"], searched["location"]
    try:
        limit = max(1, min(int(request.args.get("limit", "50")), 200))
    except ValueError:
        limit = 50
    applied = (("market", market or None), ("state", state or None),
               ("min_mw", None if min_mw is None else _json_number(min_mw)),
               ("min_kw", None if min_kw is None else _json_number(min_kw)),
               ("delivery_type", delivery_type or None), ("available_by", available_by or None),
               ("regions", regions or None),
               ("countries", _country_values(countries)[2] or None),
               ("location", location or None))
    filters = {key: value for key, value in applied if value is not None}
    try:
        rows = _db_list_listings(market=market or None, state=state or None,
                                 min_mw=min_mw, delivery_type=delivery_type or None,
                                 available_by=available_by or None, min_kw=min_kw,
                                 regions=regions or None, countries=countries or None,
                                 location=location or None, limit=limit)
        live_count = len(rows) if not filters else _db_count_live()
    except Exception as exc:
        logger.warning("[pocket-listings] list failed: %s", exc)
        return jsonify(ok=False, error="listings_unavailable", caller_tier=v["tier"]), 200

    # One terms lookup per request, and only for an identified viewer.
    terms_ok = _terms_ok(v) if v["identified"] else False
    items, needs_upgrade = [], 0
    for row in rows:
        acc = _access(row, v, _return_path(row.get("slug")), terms_ok=terms_ok)
        items.append(_teaser(row, acc))
        if acc["reason"] == "upgrade_required":
            needs_upgrade += 1

    out = {
        "ok": True,
        "citation": _citation(),
        "program": _program(live_count),
        "viewer": _viewer_public(v, "/listings"),
        "filters": filters,
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
    out = {"ok": True, "citation": _citation(),
           "terms": {"version": TERMS_VERSION, "url": TERMS_URL,
                                 "summary": TERMS_SUMMARY, "text": TERMS_TEXT}}
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp, 200


@exclusive_listings_bp.route("/api/v1/listings/terms/accept", methods=["POST"])
def accept_terms():
    """Accept the introduction terms, once per identity per TERMS_VERSION.
    Walled listing detail stays locked until the acceptance is in the lead
    register. The entry carries no PII: user_ref identifies the account."""
    _ensure_schema()
    v = _viewer()
    if not v["identified"]:
        return _err(
            401, "identity_required",
            "Sign in to accept the introduction terms — or, from an AI agent, use a "
            "key with your human's email bound (claim_free_key, then bind_email) or an "
            "OAuth connection.",
            reason=v["reason"] or "sign_in_required")
    body = request.get_json(silent=True)
    body = body if isinstance(body, dict) else {}
    if body.get("accept_terms") is not True:
        return _err(422, "terms_not_accepted",
                    "Read the introduction terms, then send accept_terms=true to accept them.",
                    terms=_terms_block())
    offered = body.get("terms_version")
    if offered not in (None, "") and str(offered) != TERMS_VERSION:
        return _err(409, "terms_version_mismatch",
                    "The introduction terms have changed — review them again.",
                    terms=_terms_block())
    allowed, retry = _rate_ok("terms", v["user_ref"], _LEAD_RATE_LIMIT)
    if not allowed:
        return _err(429, "rate_limited", "Too many requests — try again later.",
                    retry_after_s=retry)
    try:
        already = bool(_db_terms_accepted(v["user_ref"], TERMS_VERSION))
    except Exception as exc:
        logger.warning("[pocket-listings] terms lookup failed: %s", exc)
        already = False

    out = {"ok": True, "accepted": True, "already_accepted": already,
           "terms": _terms_block(), "citation": _citation()}
    if not already:
        secret = ledger.ledger_secret()
        if secret is None:
            logger.error("[pocket-listings] no ledger secret — refusing to record terms acceptance")
            return _err(503, "ledger_unavailable",
                        f"Terms acceptance cannot be recorded right now. Email {SUPPORT_EMAIL}.")
        try:
            rec = _append_event(
                secret=secret, lead_id=None, event="terms_accepted", listing=None,
                user_ref=v["user_ref"], channel=v["channel"], platform=v["platform"],
                session_hash=v["session_hash"], ip_hash=_ip_hash(), user_agent=_user_agent(),
                terms_version=TERMS_VERSION)
        except ledger.LedgerUnavailable as exc:
            logger.error("[pocket-listings] terms acceptance write failed: %s", exc)
            return _err(503, "ledger_unavailable",
                        f"Terms acceptance cannot be recorded right now. Email {SUPPORT_EMAIL}.")
        out["accepted_at"] = rec["created_at"]
        out["ledger"] = {"seq": rec["seq"], "entry_hash": rec["entry_hash"]}
    resp = jsonify(out)
    _no_store(resp)
    return resp, 200


@exclusive_listings_bp.route("/api/v1/listings/summary", methods=["GET"])
def listings_summary():
    """Live availability across the listings the teaser feed shows: how many,
    how many MW, in which markets, of which delivery types, and when they last
    changed. Never a listing's title, slug, provider or price. Public."""
    summary = cached_listings_summary()
    if summary is None:
        unavailable = jsonify({"ok": False, "error": "listings_unavailable"})
        _no_store(unavailable)
        return unavailable, 200
    out = {
        "ok": True,
        "program_status": summary["program_status"],
        "live_count": summary["live_count"],
        "total_mw": summary["total_mw"],
        "markets": summary["markets"],
        "delivery_types": summary["delivery_types"],
        "latest_updated_at": summary["latest_updated_at"],
        "generated_at": summary["generated_at"],
        "url": SITE + "/listings",
        "mcp_tool": "source_capacity",
    }
    resp = jsonify(out)
    _no_store(resp)
    return resp, 200


@exclusive_listings_bp.route("/api/v1/listings/<slug_or_id>", methods=["GET"])
def get_listing(slug_or_id):
    """One listing. Locked callers get the teaser plus the way in. An open
    listing is its specs view (_full); `disclosure` carries the provider, the
    site and its contact once the provider accepts this viewer's own
    registration on this listing, and {"released": false} for a locked caller."""
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
    access = _access(row, v, return_path, terms_ok=_terms_ok(v))
    if access["granted"] and v["identified"]:
        _record_view(row, v)
    out = {
        "ok": True,
        "citation": _citation(),
        "locked": not access["granted"],
        "listing": _full(row, access) if access["granted"] else _teaser(row, access),
        "access": access,
        "disclosure": _disclosure(row, v, access),
        "introduction": {"method": "POST",
                         "path": f"/api/v1/listings/{row.get('slug')}/intro",
                         "mcp_tool": "request_capacity_intro",
                         "operator_contact": "shared_after_acceptance"},
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
        "citation": _citation(),
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
        "citation": _citation(),
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
        "citation": _citation(),
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
    prospect's name or address, and never the provider's identity, the site or
    either side's contact. `status` is pending_email_confirmation, registered,
    operator_notified, accepted, introduced, declined or withdrawn, and a
    decision shows as its own event (registration_accepted or
    registration_declined)."""
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
        "citation": _citation(),
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


# ── provider co-marketing ────────────────────────────────────────────────

def _co_marketing_status(contact_raw):
    """linkedin_posted | website_link | missing, from contact.co_marketing."""
    record = _json_obj(contact_raw).get("co_marketing")
    if isinstance(record, dict):
        if _https_url(record.get("linkedin_post_url"), _LINKEDIN_HOSTS):
            return "linkedin_posted"
        if _https_url(record.get("website_url")):
            return "website_link"
    return "missing"


def _provider_regions(provider, row):
    """Region keys of this provider's live listings, in REGION_KEYS order; the
    listing's own region when there is no provider name to group listings by,
    or when none of them has a country canonical_stats places."""
    found = set()
    if provider:
        try:
            countries = _db_provider_live_countries(provider["name"])
        except Exception as exc:
            logger.warning("[pocket-listings] provider regions failed: %s", exc)
            countries = []
        found = {_region_of(country) for country in countries}
    keys = [key for key in REGION_KEYS if key in found]
    if keys:
        return keys
    own = _region_of(row.get("country"))
    return [own] if own else []


def _co_marketing(row, fields):
    """The post DC Hub asks the provider for. Company-level on purpose: it
    names no market, state or site, so one draft fits every listing they have,
    and its search URL carries region keys alone."""
    provider = fields["provider"]
    regions = _provider_regions(provider, row)
    search_url = f"{SITE}/listings" + (f"?region={','.join(regions)}" if regions else "")
    company = provider["name"] if provider else "[Company]"
    cadence = _CADENCE_PHRASE.get(fields["update_cadence"], "regularly")
    draft = (f"{company} now lists its available data center capacity on DC Hub Capacity "
             f"Source. Buyers and their AI agents can search it by size and location, and we "
             f"update it {cadence}.\n\nLooking for capacity? Search by kW or MW and region: "
             f"{search_url}\n\n#datacenters #AIinfrastructure")
    return {"required": True, "status": _co_marketing_status(row.get("contact")),
            "draft": draft, "search_url": search_url}


# ── the operator's decision ──────────────────────────────────────────────

def _decide(lead_id, events, listing_row, decision, channel, secret, note=None, retry=True):
    """Record one accept or decline. -> (result, error response).

    The ONE place a registration is decided: the provider's route and the
    admin status route both come through here, so the transitions, the ledger
    entry and the emails cannot drift apart. The append is guarded
    (ledger.append's unless_lead_has), so a decision that landed while this one
    was being made is never written over: the guard sends this back for a fresh
    read, and the answer describes what is actually recorded."""
    opening = _opening(events)
    if (not opening or opening.get("event") != "intro_requested" or not listing_row
            or opening.get("listing_id") != listing_row.get("id")):
        return None, _err(404, "not_found", "No registration with that id for this listing.")
    status = ledger.lead_status(events)
    if status == "withdrawn":
        return None, _err(409, "lead_withdrawn", "This registration was withdrawn.")
    if status in (None, "pending_email_confirmation"):
        return None, _err(409, "not_registered",
                          "The prospect has not confirmed this registration yet.")
    event = _DECISION_EVENT[decision]
    already = _first(events, event)
    if already is not None or (decision == "accept" and status in _RELEASED_STATUSES):
        return {"decision": decision, "status": status, "duplicate": True,
                "decided_at": _entry(already).get("created_at") if already else None,
                "ledger": ({"seq": already.get("seq"), "entry_hash": already.get("entry_hash")}
                           if already else None)}, None
    if status in _RELEASED_STATUSES or status == "declined":
        return None, _err(409, "already_decided", "This registration was already decided.",
                          status=status)
    try:
        rec = _append_event(
            secret=secret, lead_id=lead_id, event=event, listing=_listing_ref(listing_row),
            user_ref=opening.get("user_ref"), email_verified=True, channel=channel,
            meta={"note": note} if note else None,
            unless_lead_has=ledger.DECISION_BLOCKING_EVENTS)
    except ledger.LedgerConflict:
        # A decision landed between the read above and this write. Read the
        # lead again and answer from what the register now holds.
        fresh = None
        if retry:
            try:
                fresh = _db_lead_events(lead_id)
            except Exception as exc:
                logger.warning("[pocket-listings] decision re-read failed: %s", exc)
        if fresh is None:
            return None, _err(409, "already_decided", "This registration was already decided.",
                              status=status)
        return _decide(lead_id, fresh, listing_row, decision, channel, secret, note, retry=False)
    except ledger.LedgerUnavailable as exc:
        logger.error("[pocket-listings] decision write failed: %s", exc)
        return None, _err(503, "ledger_unavailable",
                          f"The decision could not be recorded. Email {SUPPORT_EMAIL}.")
    _on_decision(decision, lead_id, listing_row, opening, channel, note)
    return {"decision": decision, "status": _DECISION_STATUS[decision], "duplicate": False,
            "decided_at": rec["created_at"],
            "ledger": {"seq": rec["seq"], "entry_hash": rec["entry_hash"]}}, None


@exclusive_listings_bp.route("/api/v1/listings/<slug_or_id>/leads/<lead_id>/decision",
                             methods=["POST"])
def operator_decision(slug_or_id, lead_id):
    """The provider accepts or declines ONE registration for its listing.

    {"token": the listing's operator token — the one its ledger link carries,
     "decision": "accept" | "decline", "note": up to 500 characters}

    The token travels in the BODY and the decision is a POST: mail scanners
    prefetch links, and a prefetch must never decide anything. Accepting
    exchanges contact details and sends the buyer the site identity; declining
    shares neither side's."""
    _ensure_schema()
    allowed, retry = _rate_ok("decision", _ip_hash(), _READ_RATE_LIMIT)
    if not allowed:
        return _err(429, "rate_limited", "Too many requests — try again later.",
                    retry_after_s=retry)
    body = request.get_json(silent=True)
    body = body if isinstance(body, dict) else {}
    decision = str(body.get("decision") or "").strip().lower()
    if decision not in _DECISIONS:
        return _err(400, "invalid_request", "decision must be accept or decline",
                    allowed=list(_DECISIONS))
    note = _para(body.get("note"), _DECISION_NOTE_MAX + 1)
    if note is False or (note and len(note) > _DECISION_NOTE_MAX):
        return _err(400, "invalid_request",
                    f"note must be text of at most {_DECISION_NOTE_MAX} characters")
    secret = ledger.ledger_secret()
    if secret is None:
        return _err(503, "ledger_unavailable", "The register is unavailable right now.")
    token = str(body.get("token") or "").strip()[:200]
    try:
        row = _safe_get_listing(slug_or_id)
    except Exception:
        return _err(503, "listing_unavailable", "Listings are unavailable right now.")
    if not row or not ledger.check_operator_token(secret, row.get("id") or 0, token):
        return _err(403, "invalid_token", "This ledger link is not valid or has expired.")
    lead_id = str(lead_id or "").strip().upper()
    if not ledger.looks_like_lead_id(lead_id):
        return _err(404, "not_found", "No registration with that id for this listing.")
    try:
        events = _db_lead_events(lead_id)
    except Exception:
        return _err(503, "ledger_unavailable", "The register is unavailable right now.")
    result, error = _decide(lead_id, events, row, decision, "operator", secret, note)
    if error:
        return error
    out = {
        "ok": True,
        "citation": _citation(),
        "lead_id": lead_id,
        "listing": {"slug": row.get("slug"), "title": row.get("title")},
        "decision": result["decision"],
        "status": result["status"],
        "duplicate": result["duplicate"],
        "decided_at": result["decided_at"],
        "ledger": result["ledger"],
        "next": ("DC Hub has sent the prospect your site details and contact, and you their "
                 "details." if decision == "accept" else
                 "DC Hub has told the prospect this registration was not taken. No contact "
                 "details were shared."),
    }
    resp = jsonify(out)
    _no_store(resp)
    return resp, 200


@exclusive_listings_bp.route("/api/v1/listings/<slug_or_id>/leads", methods=["GET"])
def operator_ledger(slug_or_id):
    """The operator's view of registered leads for one listing (?token= from
    the admin operator-link endpoint, or from the link in its notices). The
    company and the requirement until the operator accepts a registration; the
    prospect's name, role, email and message once it has. `decision` says where
    to accept or decline, and `co_marketing` carries the post DC Hub asks for."""
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
        lead = _operator_lead_view(lead_id, evs)
        lead["decision"] = {"allowed": status in _DECIDABLE_STATUSES, "method": "POST",
                            "path": f"/api/v1/listings/{row.get('slug')}/leads/{lead_id}/decision"}
        leads.append(lead)
    views = [ev for ev in events if ev.get("event") == "listing_viewed"]
    out = {
        "ok": True,
        "citation": _citation(),
        "listing": {"slug": row.get("slug"), "title": row.get("title")},
        "leads": leads,
        "co_marketing": _co_marketing(row, _listing_fields(_json_obj(row.get("detail")))),
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


def _identity_warnings(title, summary, slug, provider):
    """Public text carrying an UNDISCLOSED provider's name. Title, summary and
    slug are on the teaser every caller sees before a registration is
    accepted, so a provider name there undoes the wall the listing sits
    behind. A disclosed provider is that provider's own opt-in, and warns
    about nothing."""
    if not isinstance(provider, dict) or provider.get("disclosed") is True:
        return []
    name = " ".join(str(provider.get("name") or "").split())
    if not name:
        return []
    message = ("contains the provider name, which every caller sees before a "
               "registration is accepted")
    folded, slugged, warnings = name.casefold(), _safe_slug(name), []
    for field, text in (("title", title), ("summary", summary)):
        if isinstance(text, str) and folded in " ".join(text.split()).casefold():
            warnings.append({"field": field, "message": message})
    if slugged and isinstance(slug, str) and slugged in slug.casefold():
        warnings.append({"field": "slug", "message": message})
    return warnings


def _stored_warnings(lid):
    """_identity_warnings for the listing as it stands now, so a write that
    touched only `status` is still judged on what it publishes."""
    try:
        row = _db_get_listing(str(lid))
    except Exception as exc:
        logger.warning("[pocket-listings] warning check failed: %s", exc)
        return []
    if not row:
        return []
    provider = _listing_fields(_json_obj(row.get("detail")))["provider"]
    return _identity_warnings(row.get("title"), row.get("summary"), row.get("slug"), provider)


@exclusive_listings_bp.route("/api/v1/admin/listings", methods=["GET", "POST"])
def admin_listings():
    """GET: every listing including drafts and operator contact, each with its
    co_marketing_status.
    POST: create (status defaults to draft, tier_required to registered).
    `detail` is validated by _validate_detail: 400 invalid_detail with an
    `errors` list, and nothing written, when a reserved key breaks a rule.
    `contact.co_marketing` is validated the same way (400 invalid_contact).
    The answer carries `warnings` when the title, summary or slug names an
    undisclosed provider."""
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
            r["co_marketing_status"] = _co_marketing_status(r.get("contact"))
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
    detail, detail_errors = _validate_detail(body.get("detail"))
    if detail_errors:
        return _invalid_detail(detail_errors)
    contact = body.get("contact")
    contact_errors = _validate_contact(contact)
    if contact_errors:
        return _invalid("invalid_contact", contact_errors)
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
                     json.dumps(detail) if detail is not None else None,
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
                    "tier_required": tier_required,
                    "warnings": _identity_warnings(title, body.get("summary"), slug,
                                                   (detail or {}).get("provider"))}), 200


@exclusive_listings_bp.route("/api/v1/admin/listings/<int:lid>",
                             methods=["PUT", "PATCH", "DELETE"])
def update_or_delete_listing(lid):
    """PUT/PATCH = partial update; promote with {"status": "pocket"} and open
    with {"status": "public"}. A `detail` in the body replaces the stored one
    and is validated like a create (400 invalid_detail, nothing written), and
    so is a `contact`'s co_marketing record (400 invalid_contact). The answer
    carries `warnings` when the listing, as it stands after the write, has an
    undisclosed provider's name in its title, summary or slug.
    DELETE removes the listing (ledger entries keep their own slug/title
    snapshot). One route for all three keeps the duplicate-route lint happy."""
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
    detail = None
    if "detail" in body:
        detail, detail_errors = _validate_detail(body["detail"])
        if detail_errors:
            return _invalid_detail(detail_errors)
    if "contact" in body:
        contact_errors = _validate_contact(body["contact"])
        if contact_errors:
            return _invalid("invalid_contact", contact_errors)
    settable = ("title", "summary", "status", "tier_required", "market", "state",
                "country", "latitude", "longitude", "capacity_mw", "asking_price",
                "asking_currency", "owner_id", "expires_at")
    fields, values = [], []
    for key in settable:
        if key in body:
            fields.append(f"{key} = %s")
            values.append(body[key])
    if "detail" in body:
        fields.append("detail = %s::jsonb")
        values.append(json.dumps(detail) if detail is not None else None)
    if "contact" in body:
        fields.append("contact = %s::jsonb")
        val = body["contact"]
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
                    "tier_required": got[3], "warnings": _stored_warnings(lid)}), 200


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
    if ledger.lead_status(events) in (None, "pending_email_confirmation", "withdrawn", "declined"):
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
    """Record where a lead stands: accepted | declined | introduced | withdrawn.

    accepted and declined are the OPERATOR's decision, recorded on its behalf
    with channel admin and through the same _decide as the operator's own
    route, so the transitions, the ledger entry and the emails are the same
    ones. introduced follows an acceptance: on a listing registration the
    operator has not accepted it answers 409 not_accepted, because an
    introduction the operator never agreed to would handroll the site identity
    to the prospect anyway."""
    lead_id = str(lead_id or "").strip().upper()
    body = request.get_json(silent=True) or {}
    new_status = str(body.get("status") or "").strip().lower()
    if new_status not in ("accepted", "declined", "introduced", "withdrawn"):
        return _err(400, "invalid_request",
                    "status must be accepted, declined, introduced or withdrawn")
    note = _para(body.get("note"), _DECISION_NOTE_MAX + 1)
    if note is False or (note and len(note) > _DECISION_NOTE_MAX):
        return _err(400, "invalid_request",
                    f"note must be text of at most {_DECISION_NOTE_MAX} characters")
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
    listing_row = None
    if opening.get("listing_id") is not None:
        try:
            listing_row = _db_get_listing(str(opening["listing_id"]))
        except Exception as exc:
            return _err(503, "listing_unavailable", str(exc)[:200])

    if new_status in ("accepted", "declined"):
        if opening.get("event") != "intro_requested" or opening.get("listing_id") is None:
            return _err(404, "not_found", "No listing registration with that id.")
        if not listing_row:
            return _err(404, "not_found", "The listing no longer exists.")
        decision = "accept" if new_status == "accepted" else "decline"
        result, error = _decide(lead_id, events, listing_row, decision, "admin", secret, note)
        if error:
            return error
        return jsonify({"ok": True, "lead_id": lead_id, "status": result["status"],
                        "decision": result["decision"], "duplicate": result["duplicate"],
                        "ledger": result["ledger"]}), 200

    current = ledger.lead_status(events)
    if current == "withdrawn":
        return _err(409, "lead_withdrawn", "This lead was withdrawn.")
    if current == "declined":
        return _err(409, "lead_declined", "The operator declined this registration.")
    if new_status == "introduced":
        if current == "pending_email_confirmation":
            return _err(409, "not_registered", "The prospect has not confirmed the request.")
        if opening.get("listing_id") is not None and current not in _RELEASED_STATUSES:
            return _err(409, "not_accepted",
                        "The operator has not accepted this registration yet.")
    try:
        rec = _append_event(
            secret=secret, lead_id=lead_id, event=new_status,
            listing=({"id": opening.get("listing_id"), "slug": opening.get("listing_slug"),
                      "title": opening.get("listing_title")}
                     if opening.get("listing_id") is not None else None),
            user_ref=opening.get("user_ref"), email_verified=True, channel="admin")
    except ledger.LedgerUnavailable as exc:
        return _err(503, "ledger_unavailable", str(exc)[:200])
    return jsonify({"ok": True, "lead_id": lead_id, "status": new_status, "decision": None,
                    "duplicate": False,
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
    token, url = _operator_link(secret, row)
    expires = datetime.now(timezone.utc) + timedelta(seconds=ledger.OPERATOR_TOKEN_TTL_S)
    return jsonify({
        "ok": True,
        "listing": {"id": row["id"], "slug": row.get("slug"), "title": row.get("title")},
        "url": url,
        "api": f"/api/v1/listings/{row.get('slug')}/leads?token={quote(token, safe='')}",
        "decision": f"/api/v1/listings/{row.get('slug')}/leads/<lead_id>/decision",
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
