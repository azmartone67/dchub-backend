"""Exact-location allowance — ONE meter per account per UTC calendar month.

POLICY (owner-approved 2026-09-21)
──────────────────────────────────
  anonymous           no exact location; coordinates at most 2 dp (~1.1 km)
  free / identified   the same, PLUS exact coordinates and street address for
  (and starter)       up to FREE_EXACT_LOCATIONS_PER_MONTH (default 10) DISTINCT
                      facilities per calendar month (UTC). Re-viewing a
                      facility already revealed this month is free.
  developer+          exact everywhere; this meter is never consulted
  $10 call pack       exact on every call while the pack has credits
                      (pack_active); the meter is never touched

ONE meter across the website, the REST API and MCP: every surface charges the
same table through consume(), keyed by the same account and the same facility
identity, so revealing a facility on the website makes it free over the API.

    account       the principal's email (lower-cased) when it has one, else
                  'key:' + sha256(api_key)[:16]. A caller with neither has no
                  account and cannot hold an allowance.
    facility_key  the facility's frozen canonical_slug — the identity the
                  /facilities/<slug> page is served under — via
                  routes.facility_slug_freeze.frozen_slug_for_row.

★ WHY CONSUME TAKES A LOCK FIRST. The obvious single statement,
    INSERT ... SELECT ... WHERE (count for account+period) < limit
    ON CONFLICT DO NOTHING
is NOT race-safe under READ COMMITTED for two DIFFERENT facilities: each
statement counts with its own snapshot, both see 9, both insert, and the
account ends the month on 11. ON CONFLICT only arbitrates the SAME key. So
consume() first takes a transaction-scoped advisory lock on (account, period)
in a statement of its own — a READ COMMITTED statement's snapshot is fixed when
it starts, so a lock taken INSIDE the counting statement would come too late —
and all of it is sent as ONE multi-statement execute(), which Postgres runs as
one transaction whether or not the connection is in autocommit. Two concurrent
consumes at used=9 therefore serialise: exactly one inserts. Proven against a
real Postgres in tests/test_location_meter_sql.py.

★ DDL IS NOT DONE HERE. db_utils cursors silently drop CREATE TABLE (SKIP_DDL),
so the table comes only from migrations/2026-09-21_facility_location_reveals.sql,
applied by hand. If it is missing, every call fails CLOSED — allowed=False,
measured=False — and nothing raises: a free caller gets no exact location, and
nobody gets a 500.

★ rowcount after INSERT is unreliable through db_utils.PGCursorWrapper (it runs
its own lastval() probe), so nothing here reads it: the outcome comes back in
the statement's own result row (RETURNING, then EXISTS over it).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import logging
import math
import os

logger = logging.getLogger(__name__)

TABLE = 'facility_location_reveals'
LIMIT_ENV = 'FREE_EXACT_LOCATIONS_PER_MONTH'
DEFAULT_LIMIT = 10
CHANNELS = ('web', 'api', 'mcp')

# pg_advisory_xact_lock(int4, int4): the two-key form, a lock space that does
# not overlap the single-bigint keys used elsewhere in this repo. The first key
# names this meter; the second is a hash of (account, period).
_LOCK_NAMESPACE = 0x4C4F4331          # b'LOC1'
_FACILITY_TABLES = ('discovered_facilities', 'facilities')


# ── configuration ───────────────────────────────────────────────────────────

def monthly_limit() -> int:
    """Distinct exact-location reveals per account per month. 0 disables."""
    try:
        return max(0, int(os.environ.get(LIMIT_ENV, str(DEFAULT_LIMIT))))
    except (TypeError, ValueError):
        return DEFAULT_LIMIT


def period_for(now=None) -> str:
    """The metering period: the UTC calendar month, 'YYYY-MM'."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_dt.timezone.utc)
    return now.astimezone(_dt.timezone.utc).strftime('%Y-%m')


def resets_at_for(period: str) -> str:
    """First instant of the NEXT UTC month, ISO-8601 with Z."""
    y, m = (int(p) for p in period.split('-'))
    y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return f'{y:04d}-{m:02d}-01T00:00:00Z'


# ── identity ────────────────────────────────────────────────────────────────

def account_for(principal) -> str | None:
    """The meter account for an api_tier_gating.get_request_principal() dict:
    the email when there is one, else 'key:<fingerprint>', else None."""
    if not isinstance(principal, dict):
        return None
    email = principal.get('email')
    if isinstance(email, str) and email.strip():
        return email.strip().lower()
    fp = principal.get('api_key_fingerprint')
    if isinstance(fp, str) and fp.strip():
        return 'key:' + fp.strip()
    return None


def has_location(rec) -> bool:
    """True when the record carries a usable coordinate pair. None, a non-finite
    value, an out-of-range value or (0, 0) — Null Island, the missing-coordinate
    sentinel routes.provenance.normalize_coordinates also rejects — is not."""
    if not isinstance(rec, dict):
        return False
    try:
        lat, lon = rec.get('latitude'), rec.get('longitude')
        if lat is None or lon is None or isinstance(lat, bool) or isinstance(lon, bool):
            return False
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return False
    if lat == 0.0 and lon == 0.0:
        return False
    return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0


_CANONICAL_SLUG_SQL = {
    'discovered_facilities':
        "SELECT canonical_slug FROM discovered_facilities WHERE id = %s LIMIT 1",
    'facilities':
        "SELECT canonical_slug FROM facilities WHERE id = %s LIMIT 1",
}


def _stored_canonical_slug(conn, table, row_id):
    """The row's frozen canonical_slug, or None. Never raises; rolls back on
    error so the caller's transaction stays usable (a missing column raises)."""
    sql = _CANONICAL_SLUG_SQL.get(table)
    if conn is None or sql is None or row_id is None:
        return None
    try:
        cur = conn.cursor()
        try:
            cur.execute(sql, (row_id,))
            row = cur.fetchone()
        finally:
            try:
                cur.close()
            except Exception:
                pass
        slug = row[0] if row else None
        return slug if isinstance(slug, str) and slug.strip() else None
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return None


def facility_key_for_row(row, table=None, conn=None) -> str | None:
    """The meter's facility identity: the frozen canonical_slug.

    Read from the row when it carries one (the profile page's lookup selects
    it), else from `table` by id on `conn` (the REST rows do not select it),
    else composed by the freeze module's own builder — the same fallback the
    sitemap uses for a row not yet frozen. Last resort '<table>:<id>', which
    every surface can compute for the same row. None when none of it works:
    a facility the meter cannot name cannot be charged, so it is not revealed.
    """
    if not isinstance(row, dict):
        return None
    stored = row.get('canonical_slug')
    if isinstance(stored, str) and stored.strip():
        return stored.strip()
    table = table or row.get('_src_table')
    stored = _stored_canonical_slug(conn, table, row.get('id'))
    if stored:
        return stored.strip()
    try:
        from routes.facility_slug_freeze import frozen_slug_for_row
        built = frozen_slug_for_row({'canonical_slug': None,
                                     'provider': row.get('provider'),
                                     'name': row.get('name')})
        if built:
            return built
    except Exception:
        pass
    if table in _FACILITY_TABLES and row.get('id') is not None:
        return f"{table}:{row.get('id')}"
    return None


# ── the meter ───────────────────────────────────────────────────────────────

_PEEK_SQL = """
    SELECT count(*), COALESCE(bool_or(facility_key = %s), FALSE)
      FROM facility_location_reveals
     WHERE account = %s AND period = %s
"""

# One execute(), three statements, one transaction — see the module docstring.
# SET LOCAL bounds the wait behind a concurrent consume for the same account.
_CONSUME_SQL = """
    SET LOCAL lock_timeout = '5s';
    SELECT pg_advisory_xact_lock(%s, %s);
    WITH arg AS (
        SELECT %s::text AS account, %s::text AS facility_key,
               %s::text AS period, %s::text AS channel, %s::int AS lim
    ), mine AS (
        SELECT r.facility_key
          FROM facility_location_reveals r
          JOIN arg ON r.account = arg.account AND r.period = arg.period
    ), ins AS (
        INSERT INTO facility_location_reveals (account, facility_key, period, channel)
        SELECT arg.account, arg.facility_key, arg.period, arg.channel
          FROM arg
         WHERE NOT EXISTS (SELECT 1 FROM mine WHERE mine.facility_key = arg.facility_key)
           AND (SELECT count(*) FROM mine) < arg.lim
        ON CONFLICT (account, facility_key, period) DO NOTHING
        RETURNING 1
    )
    SELECT (SELECT count(*) FROM mine),
           EXISTS (SELECT 1 FROM mine JOIN arg ON mine.facility_key = arg.facility_key),
           EXISTS (SELECT 1 FROM ins)
"""


def _lock_key(account: str, period: str) -> int:
    """A signed int32 hash of (account, period) for pg_advisory_xact_lock."""
    digest = hashlib.sha256(f'{account}|{period}'.encode('utf-8')).digest()
    return int.from_bytes(digest[:4], 'big', signed=True)


def _write_conn():
    """A pooled PRIMARY connection. Imported lazily: main is the running app."""
    from main import get_db
    return get_db()


def _result(limit, period, used, *, allowed, already=False, consumed=False,
            measured=True):
    return {
        'allowed': bool(allowed),
        'used': used,
        'remaining': (max(0, limit - used) if isinstance(used, int) else 0),
        'limit': limit,
        'period': period,
        'resets_at': resets_at_for(period),
        'already_revealed': bool(already),
        'consumed': bool(consumed),
        'measured': bool(measured),
    }


def _unmeasured(limit, period):
    """Fail CLOSED: the meter could not be read or written."""
    return _result(limit, period, None, allowed=False, measured=False)


def _run(sql, params, conn, *, commit):
    own = conn is None
    if own:
        conn = _write_conn()
    try:
        cur = conn.cursor()
        try:
            cur.execute(sql, params)
            row = cur.fetchone()
        finally:
            try:
                cur.close()
            except Exception:
                pass
        if commit:
            conn.commit()
        return row
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


def peek(account, facility_key, conn=None, now=None) -> dict:
    """Where this account stands for this facility. Never writes, never raises.

    allowed = a reveal would be granted now: already revealed this period, or
    fewer than `limit` distinct facilities revealed."""
    limit, period = monthly_limit(), period_for(now)
    if not account or not facility_key:
        return _unmeasured(limit, period)
    try:
        row = _run(_PEEK_SQL, (facility_key, account, period), conn, commit=False)
        used = int(row[0] or 0) if row else 0
        already = bool(row[1]) if row else False
        return _result(limit, period, used, allowed=(already or used < limit),
                       already=already)
    except Exception as e:
        logger.warning('location_meter.peek unavailable: %s', type(e).__name__)
        return _unmeasured(limit, period)


def consume(account, facility_key, channel, conn=None, now=None) -> dict:
    """Charge one reveal of this facility to this account's month, if allowed.

    Free when the facility was already revealed this period. Race-safe (see
    the module docstring). Never raises: any failure — including the table not
    existing — returns allowed=False, measured=False."""
    limit, period = monthly_limit(), period_for(now)
    if not account or not facility_key:
        return _unmeasured(limit, period)
    if channel not in CHANNELS:
        channel = 'api'
    params = (_LOCK_NAMESPACE, _lock_key(account, period),
              account, facility_key, period, channel, limit)
    try:
        row = _run(_CONSUME_SQL, params, conn, commit=True)
    except Exception as e:
        logger.warning('location_meter.consume unavailable: %s', type(e).__name__)
        return _unmeasured(limit, period)
    if not row:
        return _unmeasured(limit, period)
    used_before, already, inserted = int(row[0] or 0), bool(row[1]), bool(row[2])
    if already:
        return _result(limit, period, used_before, allowed=True, already=True)
    if inserted:
        return _result(limit, period, used_before + 1, allowed=True, consumed=True)
    if used_before < limit:
        # Under the limit, not already revealed, and still not inserted: the
        # INSERT met a row committed after this statement's snapshot by a
        # writer that did not take the lock. The facility IS revealed.
        return _result(limit, period, used_before + 1, allowed=True, already=True)
    return _result(limit, period, used_before, allowed=False)


def allowance_block(result) -> dict:
    """The public allowance shape: {limit, used, remaining, period, resets_at}."""
    return {k: result.get(k) for k in
            ('limit', 'used', 'remaining', 'period', 'resets_at')}


def pack_active(api_key=None, mcp_session=None) -> bool:
    """True while the caller holds unspent $10 call-pack credits.

    A pack is not a tier: grant_credit_pack writes mcp_topups keyed by the key's
    hash (and the buying MCP session) and leaves mcp_dev_keys.tier alone, so a
    pack-minted key resolves to 'free' everywhere. The entitlement is read from
    the credit ledger itself, through the one reader of it. Fails CLOSED
    (False): a ledger error sends the caller to the monthly allowance instead.
    """
    if not api_key and not mcp_session:
        return False
    try:
        from routes.mcp_conversion_plays import get_credit_status
        status = get_credit_status(api_key, mcp_session) or {}
        return int(status.get('credits') or 0) > 0
    except Exception:
        return False


# ── the REST single-record hook ─────────────────────────────────────────────

def meter_rest_record(resp, tier, table, channel='api') -> bool:
    """May THIS facility record carry exact location for THIS caller?

    Called by main.py's single-record routes before apply_record_gate, with
    the tier the route already resolved. Returns the `exact_location` flag to
    pass to the gate and stamps `_location_allowance` on the response so the
    caller can see what was charged:

        {exact, via: 'allowance', limit, used, remaining, period, resets_at}
        {exact: True, via: 'pack'}

    Only LOCATION_ALLOWANCE_TIERS with an account are metered, and only a
    record that has a location to reveal is charged. Anything that goes wrong
    returns False — the record is served at its tier's normal precision.
    """
    try:
        from util.facility_tier_gate import LOCATION_ALLOWANCE_TIERS, norm_tier
        if norm_tier(tier) not in LOCATION_ALLOWANCE_TIERS:
            return False
        rec = resp.get('data') if isinstance(resp, dict) else None
        if not has_location(rec):
            return False
        from api_tier_gating import get_request_principal, request_api_key
        principal = get_request_principal()
        if norm_tier(principal.get('tier')) not in LOCATION_ALLOWANCE_TIERS:
            return False
        account = account_for(principal)
        if not account:
            return False
        if principal.get('credential') in ('api_key', 'mcp_dev_key'):
            if pack_active(api_key=request_api_key()):
                resp['_location_allowance'] = {'exact': True, 'via': 'pack'}
                return True
        conn = _write_conn()
        try:
            fkey = facility_key_for_row(rec, table=table, conn=conn)
            res = (consume(account, fkey, channel, conn=conn) if fkey
                   else _unmeasured(monthly_limit(), period_for()))
        finally:
            try:
                conn.close()
            except Exception:
                pass
        block = allowance_block(res)
        block.update(exact=bool(res['allowed']), via='allowance')
        if not res['measured']:
            block['meter_unavailable'] = True
        resp['_location_allowance'] = block
        return bool(res['allowed'])
    except Exception as e:
        logger.warning('location_meter.meter_rest_record failed: %s', type(e).__name__)
        return False
