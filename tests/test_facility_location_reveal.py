#!/usr/bin/env python3
"""GET/POST /api/v1/facility/<slug>/location. NO NETWORK, NO DB.

The blueprint is registered on a bare Flask app (no main.py import) and its
boundaries are stubbed: the principal, the facility row, the withheld check,
the pack ledger and the meter. The meter's own SQL is proven in
tests/test_location_meter_sql.py; the principal in
tests/test_request_principal_parity.py. This file pins the DECISION — every
status, who reaches it, that coordinates appear only on an exact answer, that
GET never charges, and that nothing here is cacheable.
"""
import ast
import functools
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import flask  # noqa: E402

import api_data_protection  # noqa: E402
import api_tier_gating as atg  # noqa: E402
import routes.facility_location_reveal as rev  # noqa: E402
from util import location_meter as meter  # noqa: E402

SLUG = 'acme-alpha-one-1a2b3c4d'
URL = f'/api/v1/facility/{SLUG}/location'
ROW = {'id': 11510, 'name': 'Alpha One', 'provider': 'Acme', 'city': 'X',
       'latitude': 12.345678, 'longitude': -45.678912, 'address': ' 1 Example Way ',
       'power_mw': 300.0, 'canonical_slug': SLUG, '_src_table': 'discovered_facilities'}

FREE = {'tier': 'free', 'email': 'user@example.com', 'api_key_fingerprint': None,
        'credential': 'jwt', 'internal_key': False}
ANON = {'tier': 'anon', 'email': None, 'api_key_fingerprint': None,
        'credential': None, 'internal_key': False}


def _res(used, *, allowed, already=False, consumed=False, measured=True):
    if not measured:
        return meter._unmeasured(10, '2026-09')
    return meter._result(10, '2026-09', used, allowed=allowed, already=already,
                         consumed=consumed)


@pytest.fixture
def env(monkeypatch):
    state = {'principal': dict(FREE), 'row': dict(ROW), 'withheld': False,
             'pack': False, 'peek': _res(3, allowed=True), 'consume': None,
             'calls': [], 'honor': [], 'api_key': None}

    def principal(honor_internal_key=True):
        state['honor'].append(honor_internal_key)
        return dict(state['principal'])

    def peek(account, fkey, conn=None, now=None):
        state['calls'].append(('peek', account, fkey))
        return state['peek']

    def consume(account, fkey, channel, conn=None, now=None):
        state['calls'].append(('consume', account, fkey, channel))
        return state['consume'] or _res(4, allowed=True, consumed=True)

    monkeypatch.setattr(atg, 'get_request_principal', principal)
    monkeypatch.setattr(atg, 'request_api_key', lambda req=None: state['api_key'])
    monkeypatch.setattr(rev, '_fetch_row', lambda slug: dict(state['row']) if state['row'] else None)
    monkeypatch.setattr(rev, '_withheld', lambda row: state['withheld'])
    monkeypatch.setattr(rev, '_retry_after', lambda principal, account: 0)
    monkeypatch.setattr(meter, 'pack_active', lambda api_key=None, mcp_session=None: state['pack'])
    monkeypatch.setattr(meter, 'peek', peek)
    monkeypatch.setattr(meter, 'consume', consume)
    app = flask.Flask(__name__)
    app.register_blueprint(rev.facility_location_reveal_bp)
    state['client'] = app.test_client()
    return state


def _get(env, url=URL, **kw):
    return env['client'].get(url, **kw)


def _post(env, url=URL, **kw):
    return env['client'].post(url, **kw)


def _assert_no_location(body):
    assert (body['latitude'], body['longitude'], body['address']) == (None, None, None), body


def _assert_private(resp):
    cc = resp.headers.get('Cache-Control', '')
    assert 'private' in cc and 'no-store' in cc, cc
    vary = {v.strip().lower() for v in resp.headers.get('Vary', '').split(',')}
    assert {'authorization', 'cookie', 'x-api-key'} <= vary, resp.headers.get('Vary')


# ── every status ────────────────────────────────────────────────────────────

@pytest.mark.parametrize('tier', ['developer', 'pro', 'founding', 'team',
                                  'research_seed', 'enterprise', 'admin'])
def test_an_exact_tier_gets_the_exact_location_and_the_meter_is_untouched(env, tier):
    env['principal'] = dict(FREE, tier=tier)
    r = _get(env)
    b = r.get_json()
    assert r.status_code == 200 and b['status'] == 'exact' and b['exact_via'] == 'plan'
    assert (b['latitude'], b['longitude']) == (12.345678, -45.678912)
    assert b['address'] == '1 Example Way'
    assert b['tier'] == tier and b['allowance'] is None
    assert env['calls'] == []
    _assert_private(r)


def test_get_for_a_free_account_with_allowance_left_is_reveal_available(env):
    r = _get(env)
    b = r.get_json()
    assert r.status_code == 200 and b['status'] == 'reveal_available'
    _assert_no_location(b)
    assert b['allowance'] == {'limit': 10, 'used': 3, 'remaining': 7,
                              'period': '2026-09', 'resets_at': '2026-10-01T00:00:00Z'}
    assert env['calls'] == [('peek', 'user@example.com', SLUG)], "GET must only peek"


def test_get_at_the_limit_is_limit_reached(env):
    env['peek'] = _res(10, allowed=False)
    b = _get(env).get_json()
    assert b['status'] == 'limit_reached' and b['allowance']['remaining'] == 0
    _assert_no_location(b)
    assert all(c[0] == 'peek' for c in env['calls'])


def test_get_for_an_already_revealed_facility_is_exact_and_free(env):
    env['peek'] = _res(10, allowed=True, already=True)
    b = _get(env).get_json()
    assert b['status'] == 'exact' and b['exact_via'] == 'allowance'
    assert b['latitude'] == 12.345678
    assert [c[0] for c in env['calls']] == ['peek'], "a re-view must not be charged"


def test_post_reveals_and_charges_once_on_the_web_channel(env):
    r = _post(env)
    b = r.get_json()
    assert r.status_code == 200 and b['status'] == 'exact' and b['exact_via'] == 'allowance'
    assert (b['latitude'], b['longitude'], b['address']) == (12.345678, -45.678912, '1 Example Way')
    assert b['allowance']['used'] == 4
    assert env['calls'] == [('consume', 'user@example.com', SLUG, 'web')]
    _assert_private(r)


def test_post_at_the_limit_is_limit_reached_with_no_location(env):
    env['consume'] = _res(10, allowed=False)
    b = _post(env).get_json()
    assert b['status'] == 'limit_reached'
    _assert_no_location(b)


def test_an_unreadable_meter_fails_closed(env):
    """Table missing, pool exhausted, lock timeout: no exact, no 5xx."""
    for call in (_get, _post):
        env['peek'] = env['consume'] = _res(None, allowed=False, measured=False)
        r = call(env)
        b = r.get_json()
        assert r.status_code == 200 and b['status'] == 'limit_reached', b
        assert b['meter_unavailable'] is True
        _assert_no_location(b)


def test_starter_is_metered_exactly_like_free(env):
    env['principal'] = dict(FREE, tier='starter')
    b = _get(env).get_json()
    assert (b['status'], b['tier']) == ('reveal_available', 'starter')
    b = _post(env).get_json()
    assert (b['status'], b['tier'], b['exact_via']) == ('exact', 'starter', 'allowance')


def test_an_anonymous_caller_is_asked_to_sign_in(env):
    env['principal'] = dict(ANON)
    for call in (_get, _post):
        r = call(env)
        b = r.get_json()
        assert r.status_code == 200 and b['status'] == 'sign_in' and b['tier'] == 'anon'
        _assert_no_location(b)
        assert b['signup_url'] == f'https://dchub.cloud/signup?next=/facilities/{SLUG}'
        assert b['upgrade_url'] == 'https://dchub.cloud/pricing#developer'
        _assert_private(r)
    assert env['calls'] == [], "an anonymous caller must never reach the meter"


def test_a_free_tier_without_an_account_cannot_hold_an_allowance(env):
    env['principal'] = dict(FREE, email=None, api_key_fingerprint=None)
    b = _post(env).get_json()
    assert b['status'] == 'sign_in'
    assert env['calls'] == []


@pytest.mark.parametrize('principal', [ANON, FREE, dict(FREE, tier='enterprise')])
def test_an_operator_withheld_facility_is_withheld_for_every_tier(env, principal):
    env['principal'] = dict(principal)
    env['withheld'] = True
    for call in (_get, _post):
        b = call(env).get_json()
        assert b['status'] == 'withheld', b
        _assert_no_location(b)
    assert env['calls'] == []


@pytest.mark.parametrize('row_patch', [{'latitude': None}, {'latitude': 0, 'longitude': 0}])
def test_no_stored_coordinates_is_unknown_for_every_tier(env, row_patch):
    env['row'] = dict(ROW, **row_patch)
    for principal in (ANON, FREE, dict(FREE, tier='pro')):
        env['principal'] = dict(principal)
        b = _post(env).get_json()
        assert b['status'] == 'unknown', b
        _assert_no_location(b)
    assert env['calls'] == [], "a facility with nothing to reveal must never be charged"


def test_a_call_pack_buyer_is_exact_and_never_touches_the_meter(env):
    env['principal'] = dict(FREE, credential='api_key', email=None,
                            api_key_fingerprint='0123456789abcdef')
    env['api_key'] = 'dch_live_packbuyer'
    env['pack'] = True
    for call in (_get, _post):
        b = call(env).get_json()
        assert (b['status'], b['exact_via']) == ('exact', 'pack'), b
        assert b['address'] == '1 Example Way' and b['latitude'] == 12.345678
    assert env['calls'] == []


@pytest.mark.parametrize('url', [
    '/api/v1/facility/not-a-slug/location',
    '/api/v1/facility/../etc/passwd-1a2b3c4d/location',
    '/api/v1/facility/' + 'a' * 300 + '-1a2b3c4d/location',
])
def test_a_slug_that_names_no_facility_is_a_private_404(env, url):
    r = _get(env, url)
    assert r.status_code == 404 and r.get_json()['status'] == 'not_found'
    _assert_private(r)


def test_an_unknown_facility_is_a_404(env):
    env['row'] = None
    r = _get(env)
    assert r.status_code == 404 and r.get_json()['status'] == 'not_found'
    _assert_no_location(r.get_json())


def test_rate_limited_is_a_private_429(env, monkeypatch):
    monkeypatch.setattr(rev, '_retry_after', lambda principal, account: 17)
    r = _post(env)
    assert r.status_code == 429 and r.get_json()['status'] == 'rate_limited'
    assert r.headers['Retry-After'] == '17'
    _assert_private(r)
    assert env['calls'] == []


def test_an_unexpected_error_fails_closed_without_a_5xx(env, monkeypatch):
    env['principal'] = dict(FREE, tier='pro')

    def boom(body, row, via):
        body['latitude'] = 1.0            # half-written before the raise
        raise RuntimeError('boom')
    monkeypatch.setattr(rev, '_set_exact', boom)
    r = _get(env)
    b = r.get_json()
    assert r.status_code == 200 and b['status'] == 'unknown'
    _assert_no_location(b)
    assert 'exact_via' not in b
    _assert_private(r)


def test_the_rate_limit_keys_on_the_account_not_the_shared_mcp_egress(monkeypatch):
    seen = []
    import rate_limiter
    monkeypatch.setattr(rate_limiter, '_check', lambda key, limit, window: (seen.append(key) or (True, 1, 0)))
    app = flask.Flask(__name__)
    with app.test_request_context('/x', headers={'X-MCP-Session': 's-1'}):
        rev._retry_after({'internal_key': True}, 'user@example.com')
        rev._retry_after({'internal_key': True}, None)
        rev._retry_after({'internal_key': False}, None)
    assert seen[0] == 'facility-location:acct:user@example.com'
    assert seen[1] == 'facility-location:mcp:s-1'
    assert seen[2].startswith('facility-location:ip:')


def test_the_rate_limit_answers_with_retry_when_the_bucket_is_empty(monkeypatch):
    import rate_limiter
    monkeypatch.setattr(rate_limiter, '_check', lambda key, limit, window: (False, 0, 42))
    with flask.Flask(__name__).test_request_context('/x'):
        assert rev._retry_after({}, 'a@example.com') == 42


# ── the internal key is transport here, never identity ──────────────────────

def test_the_endpoint_resolves_the_caller_with_the_internal_key_as_transport(env):
    _get(env)
    assert env['honor'] == [False], (
        "the endpoint must call get_request_principal(honor_internal_key=False)")


def test_an_mcp_forwarded_free_key_is_metered_not_admin(monkeypatch):
    """End to end through the REAL principal: X-Internal-Key + the caller's
    free key, as the MCP worker sends it. Must be metered on channel 'mcp' —
    never 'admin', never exact without an allowance."""
    monkeypatch.setattr(atg, 'is_valid_internal_key', lambda k: k == 'ik')
    monkeypatch.setattr(atg, 'validate_api_key', lambda k: (
        {'email': None, 'plan': 'free', 'role': 'mcp'} if k == 'dch_live_free' else None))
    monkeypatch.setattr(api_data_protection, '_resolve_key_tier', lambda k: None)
    monkeypatch.setattr(atg, '_get_decode_jwt', lambda: None)
    calls = []
    monkeypatch.setattr(rev, '_fetch_row', lambda slug: dict(ROW))
    monkeypatch.setattr(rev, '_withheld', lambda row: False)
    monkeypatch.setattr(rev, '_retry_after', lambda p, a: 0)
    monkeypatch.setattr(meter, 'pack_active', lambda api_key=None, mcp_session=None: False)
    monkeypatch.setattr(meter, 'consume', lambda a, f, ch, conn=None, now=None: (
        calls.append((a, f, ch)) or _res(1, allowed=True, consumed=True)))
    monkeypatch.setattr(meter, 'peek', lambda a, f, conn=None, now=None: (
        calls.append(('peek', a)) or _res(0, allowed=True)))
    app = flask.Flask(__name__)
    app.register_blueprint(rev.facility_location_reveal_bp)
    c = app.test_client()
    b = c.get(URL, headers={'X-Internal-Key': 'ik', 'X-API-Key': 'dch_live_free'}).get_json()
    assert (b['status'], b['tier']) == ('reveal_available', 'free'), b
    b = c.post(URL, headers={'X-Internal-Key': 'ik', 'X-API-Key': 'dch_live_free'}).get_json()
    assert b['status'] == 'exact'
    import hashlib
    fp = hashlib.sha256(b'dch_live_free').hexdigest()[:16]
    assert calls[-1] == ('key:' + fp, SLUG, 'mcp')
    b = c.get(URL, headers={'X-Internal-Key': 'ik'}).get_json()
    assert (b['status'], b['tier']) == ('sign_in', 'anon'), "the internal key alone is nobody"
    _assert_no_location(b)


# ── routing: the <path:slug> route must not swallow /location ───────────────

@functools.lru_cache(maxsize=None)
def _tree(path):
    return ast.parse((ROOT / path).read_text(encoding='utf-8'))


def _route_rules(path, fn_name=None):
    """(rule, methods) for every route decorator in `path` (optionally only on
    `fn_name`), read from the source so the test uses the real rule strings."""
    tree = _tree(path)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or (fn_name and node.name != fn_name):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr == 'route' and dec.args
                    and isinstance(dec.args[0], ast.Constant)):
                methods = ['GET']
                for kw in dec.keywords:
                    if kw.arg == 'methods':
                        methods = [e.value for e in kw.value.elts]
                out.append((dec.args[0].value, methods))
    return out


def _app_with_real_facility_rules():
    app = flask.Flask(__name__)
    by_slug = _route_rules('main.py', 'facility_by_slug')
    assert ('/api/v1/facility/<path:slug>', ['GET']) in by_slug, by_slug
    for i, (rule, methods) in enumerate(by_slug):
        app.add_url_rule(rule, endpoint=f'facility_by_slug_{i}',
                         view_func=lambda slug: 'by_slug', methods=methods)
    by_id = _route_rules('main.py', 'get_facility_by_id')
    for i, (rule, methods) in enumerate(by_id):
        app.add_url_rule(rule, endpoint=f'by_id_{i}',
                         view_func=lambda facility_id: 'by_id', methods=methods)
    carriers = [r for r in _route_rules('carrier_facility_ingestion.py')
                if r[0].startswith('/api/v1/facility/')]
    assert carriers, "the /carriers suffix route moved — re-point this test"
    for i, (rule, methods) in enumerate(carriers):
        app.add_url_rule(rule, endpoint=f'carriers_{i}',
                         view_func=lambda facility_id: 'carriers', methods=methods)
    app.register_blueprint(rev.facility_location_reveal_bp)
    return app


@pytest.mark.parametrize('method', ['GET', 'POST'])
def test_location_resolves_to_this_endpoint_not_the_path_catchall(method):
    adapter = _app_with_real_facility_rules().url_map.bind('dchub.cloud')
    endpoint, args = adapter.match(URL, method=method)
    assert endpoint == 'facility_location_reveal.facility_location', endpoint
    assert args == {'slug': SLUG}


def test_the_neighbouring_routes_still_resolve_as_before():
    adapter = _app_with_real_facility_rules().url_map.bind('dchub.cloud')
    assert adapter.match(f'/api/v1/facility/{SLUG}', method='GET')[0].startswith('facility_by_slug')
    assert adapter.match('/api/v1/facility/123/carriers', method='GET')[0].startswith('carriers')
    assert adapter.match('/api/v1/facilities/8484', method='GET')[0].startswith('by_id')


def test_the_request_actually_reaches_this_endpoint(env):
    """Resolution through the test client, not only the adapter."""
    app = _app_with_real_facility_rules()
    b = app.test_client().get(URL).get_json()
    assert b and b['status'] == 'reveal_available', b
