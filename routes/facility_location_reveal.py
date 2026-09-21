"""The exact-location reveal — GET peeks, POST reveals.

    GET  /api/v1/facility/<slug>/location   where the caller stands; never charges
    POST /api/v1/facility/<slug>/location   reveal: charges one monthly allowance
                                            unless this facility is already
                                            revealed this month

HTTP 200 for every status below (404 only when the slug names no facility, 429
when the caller is over the request rate):

    {"status":      "exact" | "reveal_available" | "limit_reached" |
                    "sign_in" | "withheld" | "unknown",
     "slug", "tier",
     "latitude", "longitude", "address"   non-null ONLY when status == "exact",
     "allowance":   {"limit", "used", "remaining", "period", "resets_at"} for a
                    caller that holds a monthly allowance, else null,
     "upgrade_url": "https://dchub.cloud/pricing#developer",
     "signup_url":  "https://dchub.cloud/signup?next=/facilities/<slug>"}

    plus, when they apply: "exact_via" ('plan' | 'pack' | 'allowance') on an
    exact answer, "meter_unavailable": true when the meter could not be read.

Decided in this order (policy, owner-approved 2026-09-21):
    1. operator-withheld facility     -> withheld, for EVERY tier, paid included
    2. no coordinates stored          -> unknown
    3. developer+ (EXACT_LOCATION_TIERS) -> exact
    4. unspent $10 call-pack credits  -> exact, the meter never touched
    5. free / identified / starter with an account (util/location_meter.py):
         already revealed this month  -> exact (free)
         GET                          -> reveal_available | limit_reached
         POST                         -> exact (one charged) | limit_reached
         meter unreadable             -> limit_reached + meter_unavailable
    6. anyone else                    -> sign_in

★ WHO. The caller is resolved with get_request_principal(honor_internal_key=
False): the MCP worker sends X-Internal-Key on EVERY backend call and forwards
the end caller's key as X-API-Key, so honouring the internal key here would
make every MCP caller 'admin' and every answer exact. The internal key only
marks the channel as 'mcp' (and vouches for a forwarded X-MCP-Session, which
can carry a keyless pack buyer's credits).

★ NEVER CACHEABLE. `Cache-Control: private, no-store` and `Vary: Authorization,
Cookie, X-API-Key` on every answer, error answers included. The origin headers
are not what keeps a location out of a shared cache — the Cloudflare zone cache
has been measured serving the sibling GET /api/v1/facility/<slug> as a HIT
despite origin no-store. What does is that the edge bypasses its cache for any
request carrying X-API-Key, ?api_key, `Authorization: Bearer`, or a
dchub_token/auth_token/token cookie — every credential that can reach an
`exact` answer here — while an anonymous answer carries no location at all.
(One gap: the backend also accepts a `session_token` cookie the edge does not
count as a credential; nothing issues that cookie.) POST is never edge-cached.
"""
from __future__ import annotations

import logging
import os
import re
import time
from urllib.parse import quote

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

facility_location_reveal_bp = Blueprint('facility_location_reveal', __name__)

UPGRADE_URL = 'https://dchub.cloud/pricing#developer'
SIGNUP_URL = 'https://dchub.cloud/signup'
SIGNUP_URL_BASE = SIGNUP_URL + '?next=/facilities/'
NO_STORE_HEADERS = {
    'Cache-Control': 'private, no-store',
    'Vary': 'Authorization, Cookie, X-API-Key',
    # Belt beside the braces: what Cloudflare reads when it reads anything.
    'CDN-Cache-Control': 'no-store',
}

# A facility slug is `<body>-<8 hex>` in [a-z0-9-] (routes.facility_slug_freeze
# only ever emits that). Anything else names no facility.
_SLUG_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,240}-[0-9a-f]{8}$')

# Requests per minute per caller, on top of the app-wide limiters. Keyed by the
# meter account where there is one, so every MCP caller is not one bucket.
RPM_ENV = 'FACILITY_LOCATION_RPM'
DEFAULT_RPM = 30

_CREDENTIAL_KEYS = ('api_key', 'mcp_dev_key')


def _normalize_slug(raw):
    try:
        s = (raw or '').strip().rstrip('/').lower()
    except Exception:
        return None
    return s if _SLUG_RE.match(s) else None


def _channel(principal):
    if principal.get('internal_key'):
        return 'mcp'
    if principal.get('credential') in _CREDENTIAL_KEYS:
        return 'api'
    return 'web'


def _rpm():
    try:
        return max(1, int(os.environ.get(RPM_ENV, str(DEFAULT_RPM))))
    except (TypeError, ValueError):
        return DEFAULT_RPM


def _client_ip():
    try:
        from rate_limiter import _get_client_ip
        return _get_client_ip()
    except Exception:
        return (request.headers.get('CF-Connecting-IP')
                or (request.headers.get('X-Forwarded-For') or '').split(',')[0].strip()
                or request.remote_addr or 'unknown')


def _retry_after(principal, account):
    """Seconds to wait when this caller is over FACILITY_LOCATION_RPM, else 0.

    Reuses rate_limiter's token bucket. The limiter is DB protection, not an
    entitlement, so if it cannot be loaded the request proceeds — the meter
    behind it still fails closed on its own."""
    if account:
        key = 'acct:' + account
    elif principal.get('internal_key'):
        key = 'mcp:' + (request.headers.get('X-MCP-Session') or '')[:200]
    else:
        key = 'ip:' + str(_client_ip())
    try:
        from rate_limiter import _check
        ok, _remaining, retry = _check('facility-location:' + key, _rpm(), 60)
        return 0 if ok else max(1, int(retry or 60))
    except Exception:
        return 0


def _fetch_row(slug):
    """The row the facility page renders — the same lookup, so a reveal here
    and the page agree on which facility (and which coordinates) it is."""
    try:
        from routes.facility_profile_page import _fetch_facility_by_slug
        return _fetch_facility_by_slug(slug)
    except Exception:
        return None


# ── operator-withheld (facility_location_redactions) ────────────────────────
# The registry and its SQL functions come from
# migrations/2026-09-21_facility_location_redactions.sql, applied by hand. Until
# they exist this check answers "not withheld" — the triggers have already
# NULLed a withheld row's coordinates, so it then reads as 'unknown', never as
# a location.
_REDACTION_PROBE_SQL = (
    "SELECT to_regprocedure('facility_location_is_redacted(text[])') IS NOT NULL "
    "AND to_regprocedure('facility_location_redaction_keys("
    "text,text,text,text,text,text)') IS NOT NULL")
_WITHHELD_SQL = {
    'discovered_facilities': """
        SELECT facility_location_is_redacted(facility_location_redaction_keys(
                   'df', id::text, canonical_slug, source, source_id, source_url))
          FROM discovered_facilities WHERE id = %s LIMIT 1""",
    'facilities': """
        SELECT facility_location_is_redacted(facility_location_redaction_keys(
                   'f', id, canonical_slug, source, source_id, source_url))
          FROM facilities WHERE id = %s LIMIT 1""",
}
_PROBE_TTL = 300
_probe = {'ok': None, 'at': 0.0}


def _redaction_functions_present(cur):
    now = time.time()
    if _probe['ok'] is not None and now - _probe['at'] < _PROBE_TTL:
        return _probe['ok']
    cur.execute(_REDACTION_PROBE_SQL)
    row = cur.fetchone()
    _probe.update(ok=bool(row and row[0]), at=now)
    return _probe['ok']


def _read_conn():
    """A pooled read connection. Imported lazily: main is the running app."""
    from main import get_read_db
    return get_read_db()


def _withheld(row):
    """True when the operator asked for this facility's location to be withheld.

    Fails SOFT (False) on any error, as the policy says: the write-side trigger
    has already NULLed a withheld row's coordinates, so a failed check answers
    'unknown' downstream, never a location."""
    sql = _WITHHELD_SQL.get(row.get('_src_table'))
    if not sql or row.get('id') is None:
        return False
    conn = None
    try:
        conn = _read_conn()
        cur = conn.cursor()
        if not _redaction_functions_present(cur):
            return False
        cur.execute(sql, (row.get('id'),))
        hit = cur.fetchone()
        return bool(hit and hit[0])
    except Exception as e:
        logger.warning('facility_location: withheld check unavailable: %s',
                       type(e).__name__)
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ── the decision ────────────────────────────────────────────────────────────

def _set_exact(body, row, via):
    body['status'] = 'exact'
    body['latitude'] = float(row.get('latitude'))
    body['longitude'] = float(row.get('longitude'))
    address = row.get('address')
    body['address'] = address.strip() if isinstance(address, str) and address.strip() else None
    body['exact_via'] = via


def _decide(body, raw_slug, reveal):
    """Fill `body` in place; return the HTTP status code."""
    from api_tier_gating import get_request_principal, request_api_key
    from util import location_meter as meter
    from util.facility_tier_gate import (EXACT_LOCATION_TIERS,
                                         LOCATION_ALLOWANCE_TIERS, norm_tier)

    principal = get_request_principal(honor_internal_key=False)
    tier = norm_tier(principal.get('tier'))
    account = meter.account_for(principal)
    body['tier'] = tier

    retry = _retry_after(principal, account)
    if retry:
        body.update(status='rate_limited', retry_after=retry)
        return 429

    slug = _normalize_slug(raw_slug)
    row = _fetch_row(slug) if slug else None
    if not row:
        body['status'] = 'not_found'
        return 404
    shown = row.get('canonical_slug') or slug
    body['slug'] = shown
    body['signup_url'] = SIGNUP_URL_BASE + quote(shown, safe='-')

    if _withheld(row):
        body['status'] = 'withheld'
        return 200
    if not meter.has_location(row):
        body['status'] = 'unknown'
        return 200
    if tier in EXACT_LOCATION_TIERS:
        _set_exact(body, row, 'plan')
        return 200

    api_key = (request_api_key() if principal.get('credential') in _CREDENTIAL_KEYS
               else None)
    session = (request.headers.get('X-MCP-Session') or None
               if principal.get('internal_key') else None)
    if (api_key or session) and meter.pack_active(api_key=api_key,
                                                  mcp_session=session):
        _set_exact(body, row, 'pack')
        return 200

    if tier not in LOCATION_ALLOWANCE_TIERS or not account:
        body['status'] = 'sign_in'
        return 200

    fkey = meter.facility_key_for_row(row)
    res = (meter.consume(account, fkey, _channel(principal)) if reveal
           else meter.peek(account, fkey))
    body['allowance'] = meter.allowance_block(res)
    if not res['measured']:
        body['status'] = 'limit_reached'
        body['meter_unavailable'] = True
    elif res['already_revealed'] or (reveal and res['allowed']):
        _set_exact(body, row, 'allowance')
    elif res['allowed']:
        body['status'] = 'reveal_available'
    else:
        body['status'] = 'limit_reached'
    return 200


@facility_location_reveal_bp.route('/api/v1/facility/<path:slug>/location',
                                   methods=['GET', 'POST'])
def facility_location(slug):
    body = {
        'status': 'unknown',
        'slug': (slug or '')[:300],
        'tier': 'anon',
        'latitude': None,
        'longitude': None,
        'address': None,
        'allowance': None,
        'upgrade_url': UPGRADE_URL,
        'signup_url': SIGNUP_URL,
    }
    try:
        code = _decide(body, slug, request.method == 'POST')
    except Exception as e:
        # Fail CLOSED and never 5xx (a 5xx can trip the edge failover chain):
        # no coordinates, and a status that promises nothing.
        logger.warning('facility_location failed: %s', type(e).__name__)
        body.update(status='unknown', latitude=None, longitude=None,
                    address=None, reason='temporarily_unavailable')
        body.pop('exact_via', None)
        code = 200
    resp = jsonify(body)
    resp.status_code = code
    for header, value in NO_STORE_HEADERS.items():
        resp.headers[header] = value
    if code == 429:
        resp.headers['Retry-After'] = str(body.get('retry_after') or 60)
    return resp
