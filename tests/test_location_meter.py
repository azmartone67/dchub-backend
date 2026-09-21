#!/usr/bin/env python3
"""util/location_meter.py without a database. NO NETWORK, NO DB.

The meter's SQL is proven against a real Postgres in
tests/test_location_meter_sql.py (limit, re-view, month reset, the race). This
file pins the Python around it: identity, periods, what a DB outcome turns
into, that every failure fails CLOSED, and the REST hook's decisions — who is
metered, who is not, and that a call-pack buyer never touches the meter.
"""
import datetime as dt
import hashlib
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import api_tier_gating as atg  # noqa: E402
from util import location_meter as m  # noqa: E402

SEPT = dt.datetime(2026, 9, 21, 12, tzinfo=dt.timezone.utc)


# ── identity and periods ────────────────────────────────────────────────────

def test_the_account_is_the_email_else_the_key_fingerprint_else_nothing():
    assert m.account_for({'email': ' Ops@Example.COM ', 'api_key_fingerprint': 'ab'}) == 'ops@example.com'
    assert m.account_for({'email': None, 'api_key_fingerprint': '0123456789abcdef'}) == 'key:0123456789abcdef'
    assert m.account_for({'email': '', 'api_key_fingerprint': None}) is None
    assert m.account_for({'tier': 'free'}) is None
    assert m.account_for(None) is None


def test_the_period_is_the_utc_calendar_month():
    assert m.period_for(SEPT) == '2026-09'
    late = dt.datetime(2026, 9, 30, 23, 30, tzinfo=dt.timezone(dt.timedelta(hours=-5)))
    assert m.period_for(late) == '2026-10', "23:30 at UTC-5 is already October in UTC"
    assert m.resets_at_for('2026-09') == '2026-10-01T00:00:00Z'
    assert m.resets_at_for('2026-12') == '2027-01-01T00:00:00Z'


def test_the_limit_defaults_to_ten_and_reads_the_environment(monkeypatch):
    monkeypatch.delenv(m.LIMIT_ENV, raising=False)
    assert m.monthly_limit() == 10
    monkeypatch.setenv(m.LIMIT_ENV, '3')
    assert m.monthly_limit() == 3
    monkeypatch.setenv(m.LIMIT_ENV, 'ten')
    assert m.monthly_limit() == 10
    monkeypatch.setenv(m.LIMIT_ENV, '-4')
    assert m.monthly_limit() == 0


@pytest.mark.parametrize('rec,ok', [
    ({'latitude': 23.4567, 'longitude': -56.7891}, True),
    ({'latitude': '23.4567', 'longitude': '-56.7891'}, True),
    ({'latitude': None, 'longitude': -56.7}, False),
    ({'latitude': 0, 'longitude': 0}, False),              # Null Island
    ({'latitude': 91, 'longitude': 10}, False),
    ({'latitude': float('nan'), 'longitude': 10}, False),
    ({'latitude': True, 'longitude': 10}, False),
    ({}, False),
])
def test_only_a_real_coordinate_pair_is_a_location(rec, ok):
    assert m.has_location(rec) is ok


def test_the_facility_key_is_the_frozen_slug_else_the_builder_else_the_row():
    assert m.facility_key_for_row({'canonical_slug': 'acme-alpha-1a2b3c4d'}) == 'acme-alpha-1a2b3c4d'
    built = m.facility_key_for_row({'canonical_slug': None, 'provider': 'Acme',
                                    'name': 'Alpha One', 'id': 5})
    assert built.startswith('acme-alpha-one-') and len(built.rsplit('-', 1)[1]) == 8
    assert m.facility_key_for_row({'id': 7, 'name': None, 'provider': None,
                                   '_src_table': 'facilities'}) == 'facilities:7'
    assert m.facility_key_for_row({'name': None}) is None


class _Cur:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=None):
        self.conn.calls.append((sql, params))
        if self.conn.raise_on:
            raise self.conn.raise_on
        self.conn.last = self.conn.rows.pop(0) if self.conn.rows else None

    def fetchone(self):
        return self.conn.last

    def close(self):
        pass


class _Conn:
    """Answers with canned rows and records what it was asked. The SQL itself
    is exercised for real in test_location_meter_sql.py."""
    def __init__(self, rows=(), raise_on=None):
        self.rows, self.raise_on = list(rows), raise_on
        self.calls, self.commits, self.rollbacks, self.closed = [], 0, 0, 0
        self.last = None

    def cursor(self):
        return _Cur(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed += 1


def test_a_stored_canonical_slug_is_read_by_id_from_the_serving_table():
    conn = _Conn(rows=[('frozen-slug-deadbeef',)])
    key = m.facility_key_for_row({'id': 42, 'provider': 'P', 'name': 'N'},
                                 table='discovered_facilities', conn=conn)
    assert key == 'frozen-slug-deadbeef'
    assert conn.calls[0] == (m._CANONICAL_SLUG_SQL['discovered_facilities'], (42,))


def test_a_failed_canonical_slug_read_rolls_back_and_falls_back():
    conn = _Conn(raise_on=RuntimeError('no such column'))
    key = m.facility_key_for_row({'id': 42, 'provider': 'Acme', 'name': 'Alpha One'},
                                 table='discovered_facilities', conn=conn)
    assert key.startswith('acme-alpha-one-')
    assert conn.rollbacks == 1, "the caller's transaction must be left usable"


# ── what a DB outcome becomes ───────────────────────────────────────────────

@pytest.mark.parametrize('row,expect', [
    ((9, False, True), dict(allowed=True, used=10, remaining=0, consumed=True, already_revealed=False)),
    ((10, False, False), dict(allowed=False, used=10, remaining=0, consumed=False, already_revealed=False)),
    ((10, True, False), dict(allowed=True, used=10, remaining=0, consumed=False, already_revealed=True)),
    ((4, False, False), dict(allowed=True, used=5, remaining=5, consumed=False, already_revealed=True)),
    ((0, False, True), dict(allowed=True, used=1, remaining=9, consumed=True, already_revealed=False)),
])
def test_consume_turns_the_statement_outcome_into_an_answer(monkeypatch, row, expect):
    monkeypatch.delenv(m.LIMIT_ENV, raising=False)
    conn = _Conn(rows=[row])
    out = m.consume('a@example.com', 'fac', 'web', conn=conn, now=SEPT)
    assert {k: out[k] for k in expect} == expect
    assert out['measured'] is True and out['limit'] == 10
    assert out['period'] == '2026-09' and out['resets_at'] == '2026-10-01T00:00:00Z'
    sql, params = conn.calls[0]
    assert sql is m._CONSUME_SQL
    assert params == (m._LOCK_NAMESPACE, m._lock_key('a@example.com', '2026-09'),
                      'a@example.com', 'fac', '2026-09', 'web', 10)
    assert conn.commits == 1 and conn.closed == 0, "a borrowed connection is not closed"


def test_consume_coerces_an_unknown_channel_rather_than_failing():
    conn = _Conn(rows=[(0, False, True)])
    m.consume('a@example.com', 'fac', 'carrier-pigeon', conn=conn, now=SEPT)
    assert conn.calls[0][1][5] == 'api'


def test_peek_reads_and_never_commits(monkeypatch):
    monkeypatch.delenv(m.LIMIT_ENV, raising=False)
    conn = _Conn(rows=[(3, False)])
    out = m.peek('a@example.com', 'fac', conn=conn, now=SEPT)
    assert (out['allowed'], out['used'], out['remaining'], out['already_revealed']) == (True, 3, 7, False)
    assert conn.calls[0] == (m._PEEK_SQL, ('fac', 'a@example.com', '2026-09'))
    assert conn.commits == 0
    at_limit = m.peek('a@example.com', 'fac', conn=_Conn(rows=[(10, False)]), now=SEPT)
    assert at_limit['allowed'] is False
    revealed = m.peek('a@example.com', 'fac', conn=_Conn(rows=[(10, True)]), now=SEPT)
    assert revealed['allowed'] is True and revealed['already_revealed'] is True


def test_the_lock_is_its_own_statement_before_the_count():
    """A READ COMMITTED statement's snapshot is fixed when it starts, so a lock
    taken inside the counting statement is taken too late to serialise it."""
    stmts = [s.strip() for s in m._CONSUME_SQL.split(';') if s.strip()]
    assert stmts[0].startswith('SET LOCAL lock_timeout'), stmts[0]
    assert stmts[1] == 'SELECT pg_advisory_xact_lock(%s, %s)', stmts[1]
    assert 'count(*)' in stmts[2] and 'INSERT INTO facility_location_reveals' in stmts[2]
    assert 'ON CONFLICT (account, facility_key, period) DO NOTHING' in stmts[2]


class _UndefinedTable(Exception):
    pgcode = '42P01'


@pytest.mark.parametrize('call', ['consume', 'peek'])
def test_a_missing_table_fails_closed_and_never_raises(call):
    conn = _Conn(raise_on=_UndefinedTable('relation "facility_location_reveals" does not exist'))
    fn = m.consume if call == 'consume' else m.peek
    args = ('a@example.com', 'fac', 'web') if call == 'consume' else ('a@example.com', 'fac')
    out = fn(*args, conn=conn, now=SEPT)
    assert out['allowed'] is False and out['measured'] is False
    assert out['used'] is None and out['remaining'] == 0
    assert conn.rollbacks == 1


def test_no_connection_at_all_fails_closed(monkeypatch):
    def boom():
        raise RuntimeError('pool exhausted')
    monkeypatch.setattr(m, '_write_conn', boom)
    assert m.consume('a@example.com', 'fac', 'web', now=SEPT)['allowed'] is False
    assert m.peek('a@example.com', 'fac', now=SEPT)['measured'] is False


def test_no_account_or_no_facility_is_never_allowed():
    for acct, fkey in ((None, 'fac'), ('a@example.com', None), ('', '')):
        assert m.consume(acct, fkey, 'web', conn=_Conn(rows=[(0, False, True)]))['allowed'] is False
        assert m.peek(acct, fkey, conn=_Conn(rows=[(0, False)]))['allowed'] is False


def test_the_pack_check_fails_closed(monkeypatch):
    import routes.mcp_conversion_plays as plays
    monkeypatch.setattr(plays, 'get_credit_status', lambda k, s: {'credits': 1000, 'had_pack': True})
    assert m.pack_active(api_key='dch_live_x') is True
    monkeypatch.setattr(plays, 'get_credit_status', lambda k, s: {'credits': 0, 'had_pack': True})
    assert m.pack_active(api_key='dch_live_x') is False, "a spent pack is not an active pack"

    def boom(k, s):
        raise RuntimeError('ledger down')
    monkeypatch.setattr(plays, 'get_credit_status', boom)
    assert m.pack_active(api_key='dch_live_x') is False
    assert m.pack_active() is False


# ── the REST single-record hook ─────────────────────────────────────────────

REC = {'id': 11510, 'name': 'Alpha One', 'provider': 'Acme', 'latitude': 12.345678,
       'longitude': -45.678912, 'address': '1 Example Way', 'power_mw': 300.0}


@pytest.fixture
def rest(monkeypatch):
    """Principal, pack ledger and meter stubbed at the module boundary."""
    state = {'principal': {'tier': 'free', 'email': 'user@example.com',
                           'api_key_fingerprint': None, 'credential': 'jwt',
                           'internal_key': False},
             'pack': False, 'consumed': [], 'result': None, 'api_key': 'dch_live_k'}
    monkeypatch.setattr(atg, 'get_request_principal', lambda honor_internal_key=True: dict(state['principal']))
    monkeypatch.setattr(atg, 'request_api_key', lambda req=None: state['api_key'])
    monkeypatch.setattr(m, 'pack_active', lambda api_key=None, mcp_session=None: state['pack'])
    conn = _Conn()
    monkeypatch.setattr(m, '_write_conn', lambda: conn)

    def fake_consume(account, fkey, channel, conn=None, now=None):
        state['consumed'].append((account, fkey, channel))
        return state['result'] or m._result(10, '2026-09', 4, allowed=True, consumed=True)
    monkeypatch.setattr(m, 'consume', fake_consume)
    monkeypatch.setattr(m, 'facility_key_for_row', lambda row, table=None, conn=None: 'acme-alpha-one-1a2b3c4d')
    state['conn'] = conn
    return state


def test_a_free_account_is_charged_on_the_api_channel_and_told(rest):
    resp = {'success': True, 'data': dict(REC)}
    assert m.meter_rest_record(resp, 'free', 'discovered_facilities') is True
    assert rest['consumed'] == [('user@example.com', 'acme-alpha-one-1a2b3c4d', 'api')]
    assert resp['_location_allowance'] == {
        'limit': 10, 'used': 4, 'remaining': 6, 'period': '2026-09',
        'resets_at': '2026-10-01T00:00:00Z', 'exact': True, 'via': 'allowance'}
    assert rest['conn'].closed == 1


def test_a_refused_charge_is_reported_and_not_exact(rest):
    rest['result'] = m._result(10, '2026-09', 10, allowed=False)
    resp = {'success': True, 'data': dict(REC)}
    assert m.meter_rest_record(resp, 'identified', 'discovered_facilities') is False
    assert resp['_location_allowance']['exact'] is False
    assert resp['_location_allowance']['remaining'] == 0


def test_an_unreadable_meter_is_reported_and_not_exact(rest):
    rest['result'] = m._unmeasured(10, '2026-09')
    resp = {'success': True, 'data': dict(REC)}
    assert m.meter_rest_record(resp, 'free', 'discovered_facilities') is False
    assert resp['_location_allowance']['meter_unavailable'] is True


@pytest.mark.parametrize('tier', ['anon', 'developer', 'pro', 'enterprise', 'admin', 'bogus', None])
def test_anonymous_and_exact_tiers_are_never_metered(rest, tier):
    resp = {'success': True, 'data': dict(REC)}
    assert m.meter_rest_record(resp, tier, 'discovered_facilities') is False
    assert rest['consumed'] == [] and '_location_allowance' not in resp


def test_a_record_without_a_location_is_never_charged(rest):
    for rec in (dict(REC, latitude=None), dict(REC, latitude=0, longitude=0)):
        resp = {'success': True, 'data': rec}
        assert m.meter_rest_record(resp, 'free', 'discovered_facilities') is False
    assert rest['consumed'] == []


def test_a_caller_without_an_account_is_never_charged(rest):
    rest['principal'].update(email=None, api_key_fingerprint=None)
    assert m.meter_rest_record({'success': True, 'data': dict(REC)}, 'free', 'discovered_facilities') is False
    assert rest['consumed'] == []


def test_the_principal_must_agree_with_the_route_tier(rest):
    """The route resolved 'free' but the principal (resolved again) says anon —
    a transient lookup failure. Never charge, never reveal."""
    rest['principal'].update(tier='anon')
    assert m.meter_rest_record({'success': True, 'data': dict(REC)}, 'free', 'discovered_facilities') is False
    assert rest['consumed'] == []


def test_a_call_pack_buyer_is_exact_and_never_touches_the_meter(rest):
    """$10 pack keys resolve to their key's own tier ('free'): the pack is in
    mcp_topups, not in any tier column. Credits decide it; the meter is not
    consulted, charged or even connected to."""
    rest['principal'].update(email=None, api_key_fingerprint=hashlib.sha256(b'dch_live_k').hexdigest()[:16],
                             credential='api_key')
    rest['pack'] = True
    resp = {'success': True, 'data': dict(REC)}
    assert m.meter_rest_record(resp, 'free', 'discovered_facilities') is True
    assert resp['_location_allowance'] == {'exact': True, 'via': 'pack'}
    assert rest['consumed'] == [] and rest['conn'].closed == 0


def test_a_pack_is_only_read_for_the_key_that_authenticated(rest):
    """A JWT caller who also sends someone's X-API-Key must not borrow that
    key's pack."""
    rest['pack'] = True
    rest['principal'].update(credential='jwt')
    resp = {'success': True, 'data': dict(REC)}
    m.meter_rest_record(resp, 'free', 'discovered_facilities')
    assert resp['_location_allowance']['via'] == 'allowance'
    assert rest['consumed'], "the JWT caller should have been metered instead"
