#!/usr/bin/env python3
"""GET /api/v1/geo must RESOLVE the credential, not just see one. NO NETWORK, NO DB.

Found 2026-09-21 by the engineer gating the bulk feeds: map_geo_pins checked
only that SOME Authorization header or dchub_token cookie was present, then
returned every `facilities` row with exact coordinates and power_mw — so any
made-up header unlocked the whole table. The presence check stays (no
credential at all is still a 401); what changed is that the credential is now
resolved by api_tier_gating.get_request_tier() and the rows go through
util.facility_tier_gate.gate_records.

The handler is lifted out of main.py by ast (no test imports main.py) and run
in a real Flask request context; only the DB cursor and the credential lookups
are stubbed. The coordinates are Decimal on purpose — a NUMERIC column — which
the gate used to pass through unrounded.
"""
import ast
import json
import logging
import pathlib
import sys
import types
from decimal import Decimal

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import flask  # noqa: E402

import api_data_protection  # noqa: E402
import api_tier_gating as atg  # noqa: E402

ROWS = [
    {'id': 'f1', 'name': 'Alpha One', 'provider': 'Acme', 'city': 'X', 'state': 'Y',
     'country': 'XX', 'latitude': Decimal('12.345678'), 'longitude': Decimal('-45.678912'),
     'status': 'Operational', 'power_mw': Decimal('300.0'), 'slug': 'acme-alpha-one'},
    {'id': 'f2', 'name': 'Beta Two', 'provider': 'Acme', 'city': 'X', 'state': 'Y',
     'country': 'XX', 'latitude': 23.456789, 'longitude': -56.789123,
     'status': 'Planned', 'power_mw': None, 'slug': 'acme-beta-two'},
]
JWTS = {'jwt-dev': {'user_id': 1, 'email': 'dev@example.com'},
        'jwt-free': {'user_id': 2, 'email': 'free@example.com'}}
PLANS = {1: 'developer', 2: 'free'}


class _Cur:
    def execute(self, sql, params=None):
        assert 'FROM facilities' in sql

    def fetchall(self):
        return [dict(r) for r in ROWS]


class _Conn:
    def cursor(self, cursor_factory=None):
        return _Cur()

    def close(self):
        pass


def _load_handler():
    tree = ast.parse((ROOT / 'main.py').read_text(encoding='utf-8'))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == 'map_geo_pins')
    fn.decorator_list = []
    import psycopg2.extras  # noqa: F401  (the handler names psycopg2.extras.RealDictCursor)
    ns = {'jsonify': flask.jsonify, 'get_read_db': lambda: _Conn(),
          'psycopg2': sys.modules['psycopg2'], 'dict_from_row': dict,
          'logger': logging.getLogger('geo-test')}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), '<map_geo_pins>', 'exec'), ns)
    return ns['map_geo_pins']


@pytest.fixture
def geo(monkeypatch):
    monkeypatch.setattr(atg, 'is_valid_internal_key', lambda k: False)
    monkeypatch.setattr(atg, '_get_decode_jwt', lambda: JWTS.get)
    monkeypatch.setattr(atg, 'get_user_plan', lambda user_id=None, email=None: PLANS.get(user_id))
    monkeypatch.setattr(atg, 'validate_api_key', lambda k: None)
    monkeypatch.setattr(api_data_protection, '_resolve_key_tier', lambda k: None)
    app = flask.Flask(__name__)
    handler = _load_handler()

    def call(headers=None, cookies=None):
        h = dict(headers or {})
        if cookies:
            h['Cookie'] = '; '.join(f'{k}={v}' for k, v in cookies.items())
        with app.test_request_context('/api/v1/geo', headers=h):
            rv = handler()
            resp, status = (rv if isinstance(rv, tuple) else (rv, 200))
            return status, resp
    call.app = app
    return call


def test_no_credential_at_all_is_still_a_401(geo):
    status, resp = geo()
    assert status == 401 and resp.get_json()['error'] == 'Login required'


@pytest.mark.parametrize('headers,cookies', [
    ({'Authorization': 'Bearer made-up'}, None),
    ({'Authorization': 'anything at all'}, None),
    ({'Authorization': 'Bearer dchub_not_a_real_key'}, None),
    ({}, {'dchub_token': 'not-a-jwt'}),
])
def test_a_bogus_credential_gets_gated_rows_not_the_table(geo, headers, cookies):
    status, resp = geo(headers, cookies)
    body = resp.get_json()
    assert status == 200 and body['tier'] == 'anon' and body['_gated'] is True
    assert body['_coord_precision_dp'] == 2
    for pin in body['facilities']:
        assert 'power_mw' not in pin and 'provider' not in pin, pin
    first = body['facilities'][0]
    assert (first['latitude'], first['longitude']) == (12.35, -45.68), (
        "a NUMERIC (Decimal) coordinate passed the gate unrounded")
    assert body['count'] == len(ROWS)


def test_a_free_account_gets_the_free_ladder(geo):
    status, resp = geo({'Authorization': 'Bearer jwt-free'})
    body = resp.get_json()
    assert body['tier'] == 'free' and body['_coord_precision_dp'] == 2
    assert body['facilities'][0]['provider'] == 'Acme'
    assert 'power_mw' not in body['facilities'][0]


def test_an_exact_tier_response_is_byte_identical_to_the_ungated_one(geo):
    status, resp = geo({'Authorization': 'Bearer jwt-dev'})
    assert status == 200
    with geo.app.test_request_context('/'):
        expected = flask.jsonify({'success': True, 'facilities': [dict(r) for r in ROWS],
                                  'count': len(ROWS)}).get_data()
    assert resp.get_data() == expected, "the paid response changed"
    assert not [k for k in json.loads(resp.get_data()) if k.startswith('_') or k == 'tier']


def test_a_gate_that_cannot_load_fails_closed(geo, monkeypatch):
    import builtins
    real = builtins.__import__

    def no_resolver(name, *a, **k):
        if name == 'api_tier_gating':
            raise ImportError('simulated')
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, '__import__', no_resolver)
    status, resp = geo({'Authorization': 'Bearer jwt-dev'})
    body = resp.get_json()
    assert status == 200 and body['_gated'] is True and body['tier'] == 'anon'
    for pin in body['facilities']:
        assert 'latitude' not in pin and 'power_mw' not in pin, pin
