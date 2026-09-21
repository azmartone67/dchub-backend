#!/usr/bin/env python3
"""Exact location is its own entitlement, separate from the full record.

NO NETWORK, NO DB.

Owner decisions, 2026-09-21:
  * free/identified: same precision as anonymous (2 dp), plus EXACT
    coordinates and street address for a few facilities a month
    (util/location_meter.py) — `exact_location=True` on the gate;
  * starter ($9): keeps every paid field it had — power, operator, source —
    but its LOCATION is metered exactly like free;
  * developer and above: exact everywhere, unchanged;
  * founding / team / research_seed are paid plans (tier_registry marks them
    so) and were being served the free preview by a retyped literal.

Also pins the main.py wiring: a tested helper wired to nothing is the
failure this file exists to prevent.
"""
import ast
import builtins
import functools
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tier_registry import paid_plan_names  # noqa: E402
from util import facility_tier_gate as g  # noqa: E402

RICH = {
    'id': 11510, 'name': 'Alpha One', 'slug': 'acme-alpha-one-1a2b3c4d',
    'city': 'X', 'state': 'Y', 'country': 'XX', 'region': 'R', 'market': 'M',
    'status': 'Operational', 'provider': 'Acme', 'operator': 'Acme Ops',
    'latitude': 12.345678, 'longitude': -45.678912,
    'address': '1 Example Way', 'postal_code': '00000',
    'power_mw': 300.0, 'source': 'operator-disclosure',
    'source_url': 'https://example.invalid/pr',
    'raw_data': {'latitude': 12.345678, 'longitude': -45.678912, 'address1': '1 Example Way'},
    'fiber_providers': ['CarrierA'], 'facility_type': 'hyperscale',
}
EXACT_PLANS = sorted(set(paid_plan_names()) - {'starter'})


# ── the sets are derived, and fail closed ───────────────────────────────────

def test_the_exact_location_set_is_the_paid_canon_minus_starter_plus_admin():
    assert g.EXACT_LOCATION_TIERS == (frozenset(paid_plan_names()) - {'starter'}) | {'admin'}
    assert 'starter' not in g.EXACT_LOCATION_TIERS
    assert {'founding', 'team', 'research_seed'} <= g.EXACT_LOCATION_TIERS
    assert not ({'anon', 'free', 'identified', 'paid', 'metered'} & g.EXACT_LOCATION_TIERS)


def test_the_full_record_set_is_the_paid_canon_plus_admin_and_keeps_starter():
    assert g.PAID_TIERS == frozenset(paid_plan_names()) | {'admin'}
    assert 'starter' in g.PAID_TIERS
    for was in ('developer', 'pro', 'enterprise', 'admin', 'starter'):
        assert was in g.PAID_TIERS, f"{was} lost its full record"


def test_the_allowance_tiers_are_every_identified_tier_and_starter():
    assert g.LOCATION_ALLOWANCE_TIERS == g.IDENTIFIED_TIERS | {'starter'}
    assert not (g.LOCATION_ALLOWANCE_TIERS & g.EXACT_LOCATION_TIERS)


def test_a_broken_registry_import_fails_closed(monkeypatch):
    """No exact location for anyone, and never a wider full-record set."""
    real = builtins.__import__

    def no_registry(name, *a, **k):
        if name == 'tier_registry':
            raise ImportError('simulated')
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, '__import__', no_registry)
    assert g._derive_exact_location_tiers() == frozenset()
    assert g._derive_paid_tiers() == frozenset({'developer', 'pro', 'enterprise', 'admin', 'starter'})


def test_an_empty_registry_fails_closed(monkeypatch):
    fake = types.ModuleType('tier_registry')
    fake.paid_plan_names = lambda: ()
    monkeypatch.setitem(sys.modules, 'tier_registry', fake)
    assert g._derive_exact_location_tiers() == frozenset()
    assert 'founding' not in g._derive_paid_tiers()


# ── the ladder ──────────────────────────────────────────────────────────────

def test_free_is_anonymous_precision_by_default(monkeypatch):
    monkeypatch.delenv('MAP_ANON_COORD_DP', raising=False)
    monkeypatch.delenv('MAP_FREE_COORD_DP', raising=False)
    for tier in ('anon', 'free', 'identified', 'trial', 'starter'):
        assert g.coord_dp_for_tier(tier) == 2, tier
    for tier in EXACT_PLANS + ['admin']:
        assert g.coord_dp_for_tier(tier) is None, tier


@pytest.mark.parametrize('tier', EXACT_PLANS + ['admin'])
def test_every_exact_plan_gets_the_record_untouched(tier):
    out, n = g.gate_record(dict(RICH), tier)
    assert out == RICH and n == 0
    out, n = g.gate_record(dict(RICH), tier, exact_location=True)
    assert out == RICH and n == 0, "the flag must not change an exact plan"


# ── starter: every paid field except the location ───────────────────────────

def test_starter_keeps_power_and_operator_but_not_the_location():
    out, n = g.gate_record(dict(RICH), 'starter')
    for k in ('power_mw', 'provider', 'operator', 'source', 'fiber_providers',
              'facility_type', 'source_url'):
        assert out[k] == RICH[k], f"starter lost {k}"
    assert (out['latitude'], out['longitude']) == (12.35, -45.68)
    assert out['coordinates_status'] == 'approximate_2dp'
    for k in ('address', 'postal_code', 'raw_data'):
        assert k not in out, f"starter still receives {k}"
    assert n >= 5


def test_starter_within_its_allowance_gets_exact_coordinates_and_address():
    out, _ = g.gate_record(dict(RICH), 'starter', exact_location=True)
    assert (out['latitude'], out['longitude']) == (12.345678, -45.678912)
    assert out['address'] == '1 Example Way' and out['postal_code'] == '00000'
    assert out['power_mw'] == 300.0 and out['provider'] == 'Acme'
    assert 'raw_data' not in out, "the raw upstream record is not part of the allowance"
    assert 'coordinates_status' not in out


def test_nested_coordinates_stay_rounded_for_starter_and_are_copied_not_mutated():
    rec = dict(RICH, nearby=[{'name': 'peer', 'lat': 1.234567, 'lng': 2.345678}])
    for exact in (False, True):
        out, _ = g.gate_record(dict(rec), 'starter', exact_location=exact)
        assert out['nearby'][0]['lat'] == 1.23, "the allowance buys THIS facility only"
    assert rec['nearby'][0]['lat'] == 1.234567, "the caller's nested object was mutated"


def test_starter_markers_are_truthful():
    r = g.apply_record_gate({'success': True, 'data': dict(RICH)}, 'starter')
    assert r['_coord_precision_dp'] == 2 and r['_upgrade']['tier'] == 'starter'
    assert set(r['_withheld_fields']) == {'address', 'postal_code', 'raw_data'}
    msg = r['_upgrade']['message']
    assert 'exact location everywhere' in msg
    assert 'power capacity' not in msg, "starter already has power capacity"
    assert 'Power capacity' not in r['_upgrade_cta']


# ── free + exact_location: coordinates and address, nothing else ────────────

def test_the_allowance_adds_exactly_the_coordinates_and_the_street_address():
    coarse, _ = g.gate_record(dict(RICH), 'free')
    exact, _ = g.gate_record(dict(RICH), 'free', exact_location=True)
    added = set(exact) - set(coarse)
    assert added == {'address', 'postal_code'}, added
    assert (exact['latitude'], exact['longitude']) == (12.345678, -45.678912)
    assert (coarse['latitude'], coarse['longitude']) == (12.35, -45.68)
    for k in ('power_mw', 'source', 'source_url', 'raw_data', 'fiber_providers', 'facility_type'):
        assert k not in exact, f"the allowance leaked {k}"
    assert 'coordinates_status' not in exact


@pytest.mark.parametrize('tier', ['anon', 'anonymous', '', None, 'bogus', 'PRO ', 'paid'])
def test_the_flag_is_ignored_for_callers_that_cannot_hold_an_allowance(tier):
    out, _ = g.gate_record(dict(RICH), tier, exact_location=True)
    assert 'address' not in out and 'power_mw' not in out
    assert out.get('latitude') in (None, 12.35), out.get('latitude')


def test_an_exact_free_envelope_says_exact_and_names_what_is_withheld():
    r = g.apply_record_gate({'success': True, 'data': dict(RICH)}, 'free', exact_location=True)
    assert r['_coord_precision_dp'] is None
    assert 'power_mw' in r['_withheld_fields'] and 'address' not in r['_withheld_fields']
    assert r['data']['latitude'] == 12.345678
    for marker in ('_gated', '_upgrade', '_upgrade_cta', '_user_facing_note', '_pricing_url'):
        assert marker in r, marker


def test_developer_is_unaffected_by_all_of_this():
    r = g.apply_record_gate({'success': True, 'data': dict(RICH)}, 'developer', exact_location=True)
    assert r['data'] == RICH
    assert not [k for k in r if k.startswith('_')]


# ── the copy states the new offer, and only what the meter grants ───────────

def _strings(resp):
    return ' '.join([resp['_upgrade_cta'], resp['_user_facing_note'], resp['_upgrade']['message']])


@pytest.mark.parametrize('tier', ['anon', 'free', 'starter'])
def test_no_gate_string_promises_the_retired_110_m_rung(tier):
    text = _strings(g.apply_record_gate({'success': True, 'data': dict(RICH)}, tier))
    assert '110' not in text and 'sharpen' not in text.lower(), text


def test_the_anonymous_offer_is_the_approved_sentence(monkeypatch):
    monkeypatch.delenv('FREE_EXACT_LOCATIONS_PER_MONTH', raising=False)
    assert g.location_offer('anon') == (
        'A free account unlocks exact location for 10 facilities a month; '
        'Developer ($49/mo) unlocks exact location everywhere plus power '
        'capacity, source and nearby infrastructure.')
    assert g.location_offer('pro') == ''


def test_the_copy_follows_the_meter_limit(monkeypatch):
    monkeypatch.setenv('FREE_EXACT_LOCATIONS_PER_MONTH', '3')
    assert '3 facilities a month' in g.location_offer('free')
    monkeypatch.setenv('FREE_EXACT_LOCATIONS_PER_MONTH', '0')
    text = _strings(g.apply_record_gate({'success': True, 'data': dict(RICH)}, 'anon'))
    assert 'a month' not in text, "an allowance of 0 must not be advertised"


def test_the_precision_phrase_follows_the_dp_applied(monkeypatch):
    monkeypatch.setenv('MAP_FREE_COORD_DP', '3')
    r = g.apply_record_gate({'success': True, 'data': dict(RICH)}, 'free')
    assert r['_coord_precision_dp'] == 3 and '~110 m' in r['_user_facing_note']


# ── main.py: the helper is wired, and passes the flag through ───────────────

@functools.lru_cache(maxsize=1)
def _main_tree():
    return ast.parse((ROOT / 'main.py').read_text(encoding='utf-8'))


def _func(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise AssertionError(f'{name} not found in main.py')


@pytest.mark.parametrize('fn,expected_calls', [('facility_by_slug', 2), ('get_facility_by_id', 1)])
def test_every_single_record_branch_meters_before_it_gates(fn, expected_calls):
    """Each `_apply_record_gate(...)` in the route must receive
    exact_location=<the result of _meter_record_location(...)>, so a record is
    exact ONLY because the meter said so on that request."""
    node = _func(_main_tree(), fn)
    gated = []
    for call in ast.walk(node):
        if isinstance(call, ast.Call) and getattr(call.func, 'id', None) == '_apply_record_gate':
            kw = {k.arg: k.value for k in call.keywords}
            v = kw.get('exact_location')
            gated.append(isinstance(v, ast.Call) and getattr(v.func, 'id', None) == '_meter_record_location')
    assert gated == [True] * expected_calls, (fn, gated)


def _exec_main_helpers():
    tree = _main_tree()
    mod = types.ModuleType('_main_helpers')
    body = [_func(tree, '_apply_record_gate'), _func(tree, '_meter_record_location')]
    exec(compile(ast.Module(body=body, type_ignores=[]), '<main-helpers>', 'exec'), mod.__dict__)
    return mod


def test_the_main_py_gate_shim_passes_exact_location_through():
    mod = _exec_main_helpers()
    r = mod._apply_record_gate({'success': True, 'data': dict(RICH)}, 'free', exact_location=True)
    assert r['data']['latitude'] == 12.345678 and r['data']['address'] == '1 Example Way'
    r = mod._apply_record_gate({'success': True, 'data': dict(RICH)}, 'free')
    assert r['data']['latitude'] == 12.35 and 'address' not in r['data']


def test_the_main_py_meter_shim_fails_to_no_exact(monkeypatch):
    from util import location_meter
    mod = _exec_main_helpers()

    def boom(*a, **k):
        raise RuntimeError('meter import or DB failure')
    monkeypatch.setattr(location_meter, 'meter_rest_record', boom)
    assert mod._meter_record_location({'data': dict(RICH)}, 'free', 'discovered_facilities') is False
