#!/usr/bin/env python3
"""The exact-location policy holds on the map, search and the slug route too.

NO NETWORK, NO DB. Owner decision 2026-09-21: exact coordinates only for tiers
the record ladder calls exact (util.facility_tier_gate.coord_dp_for_tier ->
None); everyone else is coarsened on EVERY path, and exact location for a few
facilities a month is spent only through the per-facility routes.

Three surfaces still decided it for themselves after the first change:

  /api/v1/map          a hand-typed coarsen list ('anonymous','free','identified')
                       left 'starter' — and any tier it did not name — with exact
                       dots; the free rung was a map-local 3dp default; and
                       MAP_ANON_BBOX_EXACT=1 could hand anyone exact viewports.
  /api/v1/search       FACILITY_VISIBLE_FIELDS.get(tier) is None for every tier
                       it does not name, so starter got street addresses and raw
                       upstream records, and an unknown plan got the whole row.
  /api/v1/facilities/slug/<slug>
                       a live single-record route that gated but never metered.

Each block is taken from main.py's own source and executed (no test imports
main.py): the map's gating block through the same extractor
tests/test_anon_bulk_exposure.py pins the anonymous defaults with, the search
gate between its markers, and the slug route as a whole function.
"""
import ast
import builtins
import os
import pathlib
import sys
import textwrap

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import flask  # noqa: E402

import api_tier_gating as atg  # noqa: E402
from tests.test_anon_bulk_exposure import _gating_block  # noqa: E402
from tier_registry import paid_plan_names  # noqa: E402
from util import facility_tier_gate as g  # noqa: E402

GLOBAL_SWEEP, SMALL_VIEWPORT, HUGE_BBOX = None, 4.0, 400.0
EXACT = sorted((set(paid_plan_names()) - {'starter'}) | {'admin'})
KNOBS = ('MAP_ANON_COORD_DP', 'MAP_FREE_COORD_DP', 'MAP_ANON_BBOX_EXACT')


def _main_src():
    return (ROOT / 'main.py').read_text(encoding='utf-8')


# ── /api/v1/map ─────────────────────────────────────────────────────────────

def _map(tier, bbox=GLOBAL_SWEEP, env=None):
    saved = {k: os.environ.get(k) for k in KNOBS}
    for k in KNOBS:
        os.environ.pop(k, None)
    os.environ.update(env or {})
    try:
        scope = {'os': os, '_map_tier': tier, '_bbox_deg2': bbox, 'limit': 30000}
        exec(_gating_block(), scope)  # noqa: S102 — our own source under test
        return scope
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.mark.parametrize('tier', EXACT + ['internal'])
def test_every_exact_tier_gets_exact_dots_on_every_path(tier):
    for bbox in (GLOBAL_SWEEP, SMALL_VIEWPORT, HUGE_BBOX):
        assert _map(tier, bbox)['_map_exact_coords'] is True, (tier, bbox)


@pytest.mark.parametrize('tier', ['anonymous', '', 'free', 'identified', 'trial',
                                  'trial_taste', 'starter', 'bogus', 'paid'])
def test_no_other_tier_gets_an_exact_dot_on_any_path(tier):
    for bbox in (GLOBAL_SWEEP, SMALL_VIEWPORT, HUGE_BBOX):
        for env in ({}, {'MAP_ANON_BBOX_EXACT': '1'}):
            assert _map(tier, bbox, env)['_map_exact_coords'] is False, (tier, bbox, env)


@pytest.mark.parametrize('tier', ['free', 'identified', 'trial', 'starter'])
def test_the_identified_rung_is_the_record_ladder(tier):
    assert _map(tier)['_map_effective_dp'] == g.coord_dp_for_tier(tier) == 2
    moved = _map(tier, env={'MAP_FREE_COORD_DP': '4'})
    assert moved['_map_effective_dp'] == 4, "the map did not follow the ladder's knob"


def test_the_anonymous_rung_keeps_the_maps_own_default():
    assert _map('anonymous')['_map_effective_dp'] == 3, "the map's anon default moved"
    assert _map('anonymous', env={'MAP_ANON_COORD_DP': '2'})['_map_effective_dp'] == 2
    assert _map('bogus')['_map_effective_dp'] == 3, "an unknown tier must round as anonymous"


def test_a_map_without_the_ladder_fails_closed_not_blank(monkeypatch):
    real = builtins.__import__

    def no_ladder(name, *a, **k):
        if name == 'util.facility_tier_gate':
            raise ImportError('simulated')
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, '__import__', no_ladder)
    assert _map('developer')['_map_exact_coords'] is False, "fell open without the ladder"
    assert _map('starter')['_map_exact_coords'] is False
    assert _map('pro')['_map_exact_coords'] is True, "a full-record tier stays exact"


def test_a_call_pack_is_not_consulted_on_the_map():
    """Pack buyers resolve to 'free' and stay on the free rung: 'exact on every
    call' is a per-facility entitlement, the map is a sweep of the registry."""
    fn = next(n for n in ast.walk(ast.parse(_main_src()))
              if isinstance(n, ast.FunctionDef) and n.name == 'api_v1_map')
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    assert not ({'pack_active', 'get_credit_status'} & names)


# ── /api/v1/search ──────────────────────────────────────────────────────────

ROW = {
    'id': 7, 'name': 'Alpha One', 'slug': 'acme-alpha-one-1a2b3c4d', 'city': 'X',
    'state': 'Y', 'country': 'XX', 'status': 'Operational', 'provider': 'Acme',
    'market': 'M', 'latitude': 12.345678, 'longitude': -45.678912,
    'address': '1 Example Way', 'postal_code': '00000', 'power_mw': 300.0,
    'source': 'operator-disclosure', 'confidence_score': 0.9,
    'raw_data': {'latitude': 12.345678, 'address1': '1 Example Way'},
    'profile_url': 'https://dchub.cloud/facilities/acme-alpha-one-1a2b3c4d',
    'confidence_badge': 'high',
}


def _search_gate_block():
    src = _main_src()
    fn_at = src.index('def search_facilities')
    start = src.index('        # ── search row gate', fn_at)
    end = src.index('        # ── end search row gate', start)
    return textwrap.dedent(src[start:end])


def _search(monkeypatch, tier, rows=None):
    monkeypatch.setattr(atg, 'get_request_tier', lambda: tier)
    scope = {'facilities': [dict(r) for r in (rows or [ROW])]}
    exec(_search_gate_block(), scope)  # noqa: S102
    return scope


def test_search_gives_starter_its_paid_fields_but_not_the_location(monkeypatch):
    out = _search(monkeypatch, 'starter')['facilities'][0]
    for k in ('power_mw', 'provider', 'source', 'confidence_score'):
        assert out[k] == ROW[k], f"starter lost {k}"
    assert (out['latitude'], out['longitude']) == (12.35, -45.68)
    for k in ('address', 'postal_code', 'raw_data'):
        assert k not in out, f"starter still receives {k}"


@pytest.mark.parametrize('tier', EXACT)
def test_search_rows_are_untouched_for_every_exact_tier(monkeypatch, tier):
    scope = _search(monkeypatch, tier)
    assert scope['facilities'] == [ROW]
    assert scope['_splan'] == tier


@pytest.mark.parametrize('tier', ['anon', 'legacy_plan_nobody_mapped', None])
def test_search_treats_anonymous_and_unknown_tiers_alike(monkeypatch, tier):
    out = _search(monkeypatch, tier)['facilities'][0]
    assert 'power_mw' not in out and 'provider' not in out and 'raw_data' not in out
    assert (out['latitude'], out['longitude']) == (12.35, -45.68)


def test_search_fails_closed_when_the_gate_cannot_load(monkeypatch):
    real = builtins.__import__

    def no_gate(name, *a, **k):
        if name == 'util.facility_tier_gate':
            raise ImportError('simulated')
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, '__import__', no_gate)
    out = _search(monkeypatch, 'developer')['facilities'][0]
    assert set(out) <= {'name', 'city', 'country', 'status', 'slug'}, out


# ── /api/v1/facilities/slug/<slug> ──────────────────────────────────────────

SLUG_COLS = ('id', 'name', 'provider', 'city', 'state', 'country', 'region',
             'latitude', 'longitude', 'power_mw', 'status', 'address')
SLUG_ROW = (7, 'Alpha One', 'Acme', 'X', 'Y', 'XX', 'R', 12.345678, -45.678912,
            300.0, 'Operational', '1 Example Way')


class _Cur:
    description = [(c,) for c in SLUG_COLS]

    def execute(self, sql, params=None):
        assert 'FROM discovered_facilities df' in sql and params == ('1a2b3c4d',)

    def fetchone(self):
        return SLUG_ROW


class _Conn:
    def cursor(self):
        return _Cur()

    def close(self):
        pass


def _slug_route(tier, meter_says):
    tree = ast.parse(_main_src())
    body = []
    for name in ('_apply_record_gate', '_meter_record_location', 'get_facility_by_slug'):
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
        fn.decorator_list = []
        body.append(fn)
    calls = []

    def meter(resp, t, table, channel='api'):
        calls.append((t, table))
        resp['_location_allowance'] = {'exact': meter_says, 'via': 'allowance'}
        return meter_says

    from util import location_meter
    ns = {'jsonify': flask.jsonify, 'get_read_db': lambda: _Conn(),
          '_fg_tier_of_request': lambda: tier}
    exec(compile(ast.Module(body=body, type_ignores=[]), '<slug-route>', 'exec'), ns)
    return ns, calls, meter, location_meter


@pytest.mark.parametrize('tier,meter_says,exact', [
    ('free', True, True), ('starter', True, True),
    ('free', False, False), ('identified', False, False)])
def test_the_slug_route_spends_the_allowance_before_it_gates(monkeypatch, tier, meter_says, exact):
    ns, calls, meter, location_meter = _slug_route(tier, meter_says)
    monkeypatch.setattr(location_meter, 'meter_rest_record', meter)
    with flask.Flask(__name__).test_request_context('/'):
        body = ns['get_facility_by_slug']('acme-alpha-one-1a2b3c4d').get_json()
    assert calls == [(tier, 'discovered_facilities')]
    data = body['data']
    if exact:
        assert (data['latitude'], data['longitude'], data['address']) == (
            12.345678, -45.678912, '1 Example Way')
        assert body.get('_coord_precision_dp') is None
    else:
        assert (data['latitude'], data['longitude']) == (12.35, -45.68)
        assert 'address' not in data
        assert body['_coord_precision_dp'] == 2
    if tier == 'starter':
        assert data['power_mw'] == 300.0 and data['provider'] == 'Acme'
    else:
        assert 'power_mw' not in data, "the allowance buys the location only"
    assert body['_location_allowance']['exact'] is exact
    # Markers say what was withheld. A starter record made exact by its
    # allowance withholds nothing on this route (it selects no raw_data), and
    # `_upgrade` absent is the documented signal for exactly that.
    withheld_something = not (tier == 'starter' and exact)
    for marker in ('_gated', '_upgrade', '_withheld_fields'):
        assert (marker in body) is withheld_something, marker


def test_the_slug_route_leaves_an_exact_tier_whole_and_unmetered(monkeypatch):
    ns, calls, _meter, location_meter = _slug_route('developer', True)
    # The real hook, not the stub: it must refuse an exact tier before any DB.
    with flask.Flask(__name__).test_request_context('/'):
        body = ns['get_facility_by_slug']('acme-alpha-one-1a2b3c4d').get_json()
    assert body == {'success': True, 'data': dict(zip(SLUG_COLS, SLUG_ROW))}


def test_an_allowance_answer_cannot_make_anonymous_exact(monkeypatch):
    """The route calls the hook for every tier and the real hook refuses anon
    (tests/test_location_meter.py). Belt and braces: even a hook that answered
    'allowed' must not make an anonymous record exact — the gate ignores the
    flag for a tier that cannot hold an allowance."""
    ns, calls, meter, location_meter = _slug_route('anon', True)
    monkeypatch.setattr(location_meter, 'meter_rest_record', meter)
    with flask.Flask(__name__).test_request_context('/'):
        body = ns['get_facility_by_slug']('acme-alpha-one-1a2b3c4d').get_json()
    assert (body['data']['latitude'], body['data']['longitude']) == (12.35, -45.68)
    assert 'address' not in body['data'] and 'provider' not in body['data']
