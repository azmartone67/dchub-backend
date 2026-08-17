"""map_tier_gating._tier_from_refresh_cookie — the server-render half of the
lapsed-Pro lockout (2026-08-17).

THE BUG. The access JWT lives 7 days inside a `dchub_token` cookie that lives
30; the `dchub_refresh` cookie lives 90. On days 7-30 a returning payer presents
a DEAD JWT, so _detect_caller_tier fell through to 'anonymous' and every
SERVER-RENDERED tier page locked itself against a paying subscriber. Measured
against prod with a minted pro JWT for a real pro account (admin001, plan=pro):

    valid   dchub_token cookie -> /dcpi renders 0 lock icons
    expired dchub_token cookie -> /dcpi renders 75 lock icons +
                                  "Showing 25 of 323 markets · Claim free key"

A client-side refresh cannot rescue a server render — the HTML is already locked
before any JS runs. So the render reads the credential that IS still valid: the
DB-backed refresh cookie.

WHAT THESE TESTS FENCE — the two ways this fix could become a liability:

  1. IT MUST NOT BECOME A PAYWALL HOLE. Every disqualifier fails closed:
     unknown hash, expired, revoked, already-rotated. A forged cookie value
     resolves to nothing. (This is precisely why `dchub_session` may NOT be used
     for this: it carries no user id and any browser can obtain one. A refresh
     token is a 48-byte secret whose sha256 is a row bound to a users.id.)

  2. IT MUST NEVER WRITE. POST /api/auth/refresh ROTATES and treats a replayed
     token as theft by revoking the user's ENTIRE chain — a full logout. Page
     renders happen concurrently (tabs, prefetch, crawlers), so a rotation here
     would mass-revoke live sessions. test_never_mutates asserts the executed
     SQL contains no INSERT/UPDATE/DELETE and that commit() is never called.
     If someone "helpfully" adds a last_used touch, this test goes red.

  3. Anonymous traffic must not pay a DB round-trip (it also keeps the /dcpi
     anonymous render edge-cacheable): with no cookie, the DB is never opened.
"""
import hashlib
import sys
import types

import pytest

import map_tier_gating


RAW = 'a-real-looking-refresh-secret-value'
HASHED = hashlib.sha256(RAW.encode()).hexdigest()


class FakeCursor:
    def __init__(self, row):
        self._row = row
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._row


class FakeConn:
    def __init__(self, row):
        self.cursor_obj = FakeCursor(row)
        self.commits = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1


@pytest.fixture
def wire(monkeypatch):
    """Install a fake `main` module exposing the pg helpers the resolver imports,
    and report how many connections were opened."""
    state = {'opened': 0, 'returned': 0, 'conn': None}

    def _install(row):
        conn = FakeConn(row)
        state['conn'] = conn

        fake_main = types.ModuleType('main')

        def get_pg_connection(retries=1):
            state['opened'] += 1
            return conn

        def return_pg_connection(c):
            state['returned'] += 1

        fake_main.get_pg_connection = get_pg_connection
        fake_main.return_pg_connection = return_pg_connection
        monkeypatch.setitem(sys.modules, 'main', fake_main)
        return state

    return _install


def _with_cookies(cookies):
    """Minimal Flask request context carrying the given cookies."""
    from flask import Flask
    app = Flask(__name__)
    jar = '; '.join(f'{k}={v}' for k, v in cookies.items())
    return app.test_request_context('/dcpi', headers={'Cookie': jar} if jar else {})


def test_live_pro_refresh_cookie_resolves_pro(wire):
    st = wire(('admin001', 'owner@dchub.cloud', 'pro'))
    with _with_cookies({'dchub_refresh': RAW}):
        plan, info = map_tier_gating._tier_from_refresh_cookie()
    assert plan == 'pro', 'a live refresh cookie for a pro account must resolve pro'
    assert info['user_id'] == 'admin001'
    assert info['source'] == 'refresh_cookie'
    # It must look the token up by HASH, never by the raw secret.
    sql, params = st['conn'].cursor_obj.executed[0]
    assert params == (HASHED,), 'lookup must be by sha256(token), not the raw value'
    assert RAW not in sql


def test_free_plan_stays_free(wire):
    wire(('u2', 'free@example.com', 'free'))
    with _with_cookies({'dchub_refresh': RAW}):
        plan, _ = map_tier_gating._tier_from_refresh_cookie()
    assert plan == 'free', 'a free account must not be upgraded by this path'


def test_no_cookie_never_touches_the_db(wire):
    st = wire(('admin001', 'owner@dchub.cloud', 'pro'))
    with _with_cookies({}):
        plan, info = map_tier_gating._tier_from_refresh_cookie()
    assert (plan, info) == (None, None)
    assert st['opened'] == 0, 'anonymous traffic must not pay a DB round-trip'


def test_unknown_or_forged_cookie_fails_closed(wire):
    # No matching row — the SQL already filters expired/revoked/replaced, so a
    # disqualified token is indistinguishable from an unknown one here.
    st = wire(None)
    with _with_cookies({'dchub_refresh': 'forged-value'}):
        plan, info = map_tier_gating._tier_from_refresh_cookie()
    assert (plan, info) == (None, None), 'a forged refresh cookie must grant nothing'
    assert st['opened'] == 1


def test_sql_filters_every_disqualifier(wire):
    """The fail-closed conditions live in the SQL, so assert they are present.
    Dropping any one of them would authorise an expired, revoked or rotated
    token — i.e. a real paywall hole with no test failure elsewhere."""
    wire(('admin001', 'owner@dchub.cloud', 'pro'))
    with _with_cookies({'dchub_refresh': RAW}):
        map_tier_gating._tier_from_refresh_cookie()
    sql = ' '.join(_normalise(map_tier_gating_last_sql()).split())
    assert 'expires_at > now()' in sql, 'must reject expired tokens'
    assert 'revoked_at is null' in sql, 'must reject revoked tokens'
    assert 'replaced_by is null' in sql, 'must reject already-rotated tokens'


def test_never_mutates(wire):
    """READ-ONLY is load-bearing: rotating here would trip the reuse detector in
    /api/auth/refresh and revoke the user's whole chain (full logout)."""
    st = wire(('admin001', 'owner@dchub.cloud', 'pro'))
    with _with_cookies({'dchub_refresh': RAW}):
        map_tier_gating._tier_from_refresh_cookie()
    for sql, _ in st['conn'].cursor_obj.executed:
        low = sql.lower()
        for verb in ('insert', 'update', 'delete', 'truncate'):
            assert verb not in low, f'resolver must never {verb.upper()} — it would rotate/revoke'
    assert st['conn'].commits == 0, 'a read-only resolver must not commit'
    assert st['returned'] == 1, 'the pooled connection must always be returned'


def test_db_failure_fails_closed(monkeypatch):
    """A DB hiccup must deny, never grant."""
    fake_main = types.ModuleType('main')

    def boom(retries=1):
        raise RuntimeError('pool exhausted')

    fake_main.get_pg_connection = boom
    fake_main.return_pg_connection = lambda c: None
    monkeypatch.setitem(sys.modules, 'main', fake_main)
    with _with_cookies({'dchub_refresh': RAW}):
        plan, info = map_tier_gating._tier_from_refresh_cookie()
    assert (plan, info) == (None, None)


# ── helpers ────────────────────────────────────────────────────────────
_LAST = {}


def _normalise(s):
    return s.lower()


def map_tier_gating_last_sql():
    return _LAST['sql']


@pytest.fixture(autouse=True)
def _capture_sql(monkeypatch):
    """Record the SQL the resolver executes so test_sql_filters_every_disqualifier
    can assert on it without reaching into fixture internals."""
    orig = FakeCursor.execute

    def spy(self, sql, params=None):
        _LAST['sql'] = sql
        return orig(self, sql, params)

    monkeypatch.setattr(FakeCursor, 'execute', spy)
