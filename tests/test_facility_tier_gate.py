#!/usr/bin/env python3
"""The facility-record tier gate. NO NETWORK, NO DB.

Guards the fix for the registry export measured live 2026-09-19, anonymous,
no key and no cookie:

    GET /api/v1/map?limit=25000        -> 20,139 slugs, 4.96 MB, tier=anonymous
    GET /api/v1/facility/<slug>        -> power_mw 2300.0, provider "Microsoft",
                                          address, fiber_providers, coords 6 dp
    GET /api/v1/search?q=ashburn       -> tier=anon, coords 6 dp
    GET /api/facilities?limit=200      -> 100 rows, coords 4 dp

The first request is the index; the rest are the record. Together they are the
whole proprietary registry, free, at ~0.1 m precision.

★ WHY THE ENV-VAR TEST BELOW IS THE LOAD-BEARING ONE
The coordinate ladder was never missing — it was LOCAL to /api/v1/map's handler
(_MAP_ANON_COORD_DP / _MAP_FREE_COORD_DP). The corpus routes reused the FIELD
mask, which contains latitude and longitude with no precision constraint, and
so passed a field audit while serving survey-grade coordinates. The fix is only
durable if the shared module and the map keep reading ONE pair of knobs;
test_reads_the_maps_own_env_knobs fails if either side is renamed.
"""
import ast
import bisect
import functools
import os
import re
import sys
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from util.facility_tier_gate import (  # noqa: E402
    MINIMAL_ANON_FIELDS,
    coarsen_coords_deep,
    coord_dp_for_tier,
    gate_record,
    gate_records,
    visible_fields_for_tier,
)

# A record shaped like discovered_facilities, with every paid column populated.
# Coordinates are deliberately at 6 dp — the precision actually measured.
RICH = {
    'id': 11510,
    'name': 'Microsoft Mount Pleasant AI Campus',
    'slug': 'microsoft-microsoft-mount-pleasant-ai-campus-ed53212e',
    'city': 'Mount Pleasant', 'state': 'WI', 'country': 'US',
    'region': 'Mount Pleasant', 'market': 'Mount Pleasant',
    'status': 'Under Construction',
    'latitude': 42.726841, 'longitude': -87.883912,
    'power_mw': 2300.0,
    'provider': 'Microsoft',
    'address': '1 Innovation Way',
    'fiber_providers': ['Microsoft'],
    'fiber_carrier_count': 1,
    'source': 'operator-disclosure',
    'source_url': 'https://example.invalid/pr',
    'raw_data': {'internal': 'do not ship'},
    'confidence_score': 0.91,
    'investment_usd': 3_300_000_000,
    'facility_type': 'hyperscale',
    'sqft': 1_200_000,
    'is_duplicate': 0,
}

# Every column that is the paid product. None of these may reach a non-paying
# tier through any route.
PAID_COLUMNS = (
    'power_mw', 'address', 'fiber_providers', 'fiber_carrier_count',
    'source', 'source_url', 'raw_data', 'confidence_score',
    'investment_usd', 'facility_type', 'sqft',
)


def test_anon_gets_no_paid_column():
    out, n = gate_record(dict(RICH), 'anon')
    leaked = [c for c in PAID_COLUMNS if c in out]
    assert leaked == [], f"anonymous caller still receives {leaked}"
    assert n > 0, "gate reported zero redactions on a fully-populated record"


def test_anon_coordinates_are_coarsened_to_the_map_ladder():
    out, _ = gate_record(dict(RICH), 'anon')
    dp = coord_dp_for_tier('anon')
    assert dp is not None, "anonymous must not be exact"
    # The value must be the ROUNDED one, not merely present.
    assert out['latitude'] == round(RICH['latitude'], dp)
    assert out['longitude'] == round(RICH['longitude'], dp)
    # And it must actually differ from the raw value, or the assertion above is
    # satisfied by a fixture that was already coarse.
    assert out['latitude'] != RICH['latitude']


def test_free_is_a_real_rung_between_anon_and_paid():
    """Signing up must buy something visible, and not everything.

    Since 2026-09-21 (owner decision) the visible thing is NOT a sharper blur:
    a free record rounds exactly like an anonymous one, and what the account
    buys is EXACT location for a few facilities a month — exact_location=True,
    spent through util/location_meter.py — which anonymous can never spend.
    """
    anon, _ = gate_record(dict(RICH), 'anon')
    free, _ = gate_record(dict(RICH), 'free')
    free_exact, _ = gate_record(dict(RICH), 'free', exact_location=True)
    anon_exact, _ = gate_record(dict(RICH), 'anon', exact_location=True)
    paid, _ = gate_record(dict(RICH), 'developer')

    # Free buys the operator name...
    assert 'provider' not in anon
    assert free.get('provider') == 'Microsoft'
    # ...the same precision as anonymous by default...
    assert coord_dp_for_tier('free') == coord_dp_for_tier('anon')
    assert free['latitude'] == anon['latitude'] != RICH['latitude']
    # ...and the exact location + street address once its allowance is spent.
    assert free_exact['latitude'] == RICH['latitude']
    assert free_exact['address'] == RICH['address']
    assert anon_exact['latitude'] != RICH['latitude'] and 'address' not in anon_exact
    # It does NOT buy the field people pay $49 for, allowance or not.
    assert 'power_mw' not in free and 'power_mw' not in free_exact
    # Paid gets the record whole, coordinates untouched.
    assert paid['power_mw'] == 2300.0
    assert paid['latitude'] == RICH['latitude']


def test_paid_record_is_not_copied_or_rounded():
    out, n = gate_record(dict(RICH), 'pro')
    assert n == 0
    assert out['latitude'] == RICH['latitude']
    assert out['raw_data'] == RICH['raw_data']


def test_unknown_tier_string_is_treated_as_anonymous_not_paid():
    """A typo in a plan name must not be a privilege escalation."""
    for bogus in ('devloper', 'PRO ', 'premium', 'x', ''):
        out, _ = gate_record(dict(RICH), bogus)
        assert 'power_mw' not in out, f"tier {bogus!r} was treated as paid"


def test_redaction_count_is_not_inflated():
    """A tally that counts non-secrets cannot detect a no-op.

    An already-coarse, already-thin record must report ZERO — otherwise a
    caller asserting `n > 0` would be satisfied by a payload that leaked
    nothing and the no-op check would be worthless.
    """
    thin = {'name': 'X', 'city': 'Y', 'state': 'Z', 'country': 'US',
            'status': 'Operational', 'slug': 's', 'latitude': 42.72,
            'longitude': -87.88}
    out, n = gate_record(dict(thin), 'anon')
    assert n == 0, f"gate claimed {n} redactions on a record with no secrets"
    assert out['latitude'] == 42.72


def test_empty_paid_columns_do_not_count_as_redactions():
    sparse = dict(RICH, power_mw=None, address='', fiber_providers=[],
                  raw_data={}, source=None, source_url=None,
                  confidence_score=None, investment_usd=None,
                  facility_type=None, sqft=None, fiber_carrier_count=None)
    _, n = gate_record(sparse, 'anon')
    # Only the coordinate roundings and `is_duplicate`/`market` carry values.
    assert n < len(PAID_COLUMNS), (
        "empty columns are being counted as redactions, which would let a "
        "data-poor row report a healthy tally while a rich row leaks")


def test_gate_fails_closed_when_the_field_mask_cannot_be_imported(monkeypatch):
    """An ImportError must not be the thing that grants full access.

    /api/facilities shipped `except Exception: pass  # fail-open on import
    error` over exactly this path, on a handler that SELECTs power_mw.
    """
    import builtins
    real_import = builtins.__import__

    def boom(name, *a, **k):
        if name == 'api_tier_gating':
            raise ImportError('simulated')
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, '__import__', boom)
    visible = visible_fields_for_tier('anon')
    assert visible is not None, "fell through to None, which means FULL RECORD"
    assert set(visible) == set(MINIMAL_ANON_FIELDS)
    out, n = gate_record(dict(RICH), 'anon')
    assert 'power_mw' not in out
    assert n > 0


def test_kill_switch_still_works_and_is_the_maps_kill_switch():
    """MAP_ANON_COORD_DP=6 was the documented no-deploy kill switch."""
    prev = os.environ.get('MAP_ANON_COORD_DP')
    os.environ['MAP_ANON_COORD_DP'] = '6'
    try:
        out, _ = gate_record(dict(RICH), 'anon')
        assert out['latitude'] == RICH['latitude']
    finally:
        if prev is None:
            os.environ.pop('MAP_ANON_COORD_DP', None)
        else:
            os.environ['MAP_ANON_COORD_DP'] = prev


def test_deep_coarsen_reaches_nested_records():
    payload = {'data': [{'latitude': 1.234567, 'longitude': 2.345678}],
               'nearby': {'rows': [{'lat': 3.456789, 'lng': 4.567891}]},
               'provenance': {'cite_as': 'DC Hub'}}
    n = coarsen_coords_deep(payload, 'anon')
    dp = coord_dp_for_tier('anon')
    assert n == 4
    assert payload['data'][0]['latitude'] == round(1.234567, dp)
    assert payload['nearby']['rows'][0]['lng'] == round(4.567891, dp)
    assert payload['provenance']['cite_as'] == 'DC Hub'


def test_deep_coarsen_is_a_noop_for_paid():
    payload = {'data': [{'latitude': 1.234567}]}
    assert coarsen_coords_deep(payload, 'enterprise') == 0
    assert payload['data'][0]['latitude'] == 1.234567


def test_deep_coarsen_survives_a_self_referencing_payload():
    d = {'latitude': 1.234567}
    d['self'] = d
    assert coarsen_coords_deep(d, 'anon') == 1


def test_booleans_are_not_mistaken_for_coordinates():
    rec = {'name': 'X', 'latitude': True}
    out, _ = gate_record(rec, 'anon')
    assert out['latitude'] is True


# ─────────────────────────────────────────────────────────────────────────────
# The wiring. These read main.py's AST, not its text: a comment naming the
# module must not be able to satisfy them.
# ─────────────────────────────────────────────────────────────────────────────

# ★ get_facility_by_slug was MISSING from this list and the omission survived a
# mutation run: un-gating it left every test green, because the coverage check
# below accepted a bare IMPORT and the handler's unreachable fail-closed
# fallback still imports the module. Both holes are closed — the list is
# complete, and coverage now requires a CALL.
GATED_HANDLERS = ('facility_by_slug', 'get_facilities', 'get_facility_by_id',
                  'search_facilities', 'get_facility_by_slug')


@functools.lru_cache(maxsize=None)
def _main_tree():
    """Parsed once per process: every test here only reads the tree."""
    return ast.parse((ROOT / 'main.py').read_text(encoding='utf-8'))


# The two class-wide guards below look only at defs whose source holds all
# three of these. ast.get_source_segment re-splits all of main.py on every call,
# and paying that for each of its thousands of defs cost ~35 s, so a def is
# only measured once its physical lines hold all three (a necessary condition:
# none of them contains a line break).
_CLASS_MARKERS = ('discovered_facilities', 'power_mw', 'latitude')


def _marker_lines(src, markers=_CLASS_MARKERS):
    breaks = [m.end() for m in re.finditer(r"\r\n|\r|\n", src)]
    return [sorted({bisect.bisect_right(breaks, m.start()) + 1
                    for m in re.finditer(re.escape(k), src)}) for k in markers]


def _spans_all(node, marker_lines):
    for lines in marker_lines:
        i = bisect.bisect_left(lines, node.lineno)
        if i == len(lines) or lines[i] > node.end_lineno:
            return False
    return True


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name}() not found in main.py — renamed?")


@pytest.mark.parametrize('fn', GATED_HANDLERS)
def test_handler_actually_imports_the_gate(fn):
    """An ImportFrom NODE, so prose in a comment cannot satisfy this."""
    tree = _main_tree()
    node = _func(tree, fn)

    def _imports(n):
        return {i.module for i in ast.walk(n) if isinstance(i, ast.ImportFrom)}

    mods = _imports(node)
    # r-slashparity (2026-09-20): follow ONE level of indirection. The
    # single-record routes now import nothing themselves — they call
    # _apply_record_gate, a module-level helper in main.py that does the
    # import. The property this guard means ("the gate is in the handler's
    # path") still holds; requiring the ImportFrom node to sit literally inside
    # the handler would have forced the policy back into the routes, which is
    # what produced three copies of it.
    if 'util.facility_tier_gate' not in mods:
        called = {c.func.id for c in ast.walk(node)
                  if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        for helper in called:
            try:
                h = _func(tree, helper)
            except AssertionError:
                continue
            mods |= _imports(h)
    assert 'util.facility_tier_gate' in mods, (
        f"{fn}() does not reach util.facility_tier_gate, directly or through a "
        f"main.py helper — the gate is not in its path, whatever the comments say")


@pytest.mark.parametrize('fn', GATED_HANDLERS)
def test_handler_calls_the_gate(fn):
    node = _func(_main_tree(), fn)
    called = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                called.add(f.id)
            elif isinstance(f, ast.Attribute):
                called.add(f.attr)
    # r-slashparity (2026-09-20): the single-record routes no longer call
    # gate_record directly — they call _apply_record_gate, the one envelope in
    # main.py that delegates to util.facility_tier_gate.apply_record_gate.
    # Naming both spellings keeps this guard measuring "the gate is in the
    # path" rather than "this exact symbol appears".
    gate_calls = {'gate_record', 'gate_records', 'coarsen_coords_deep',
                  '_fg_rec', '_fg_rows', '_sc_deep', '_fid_gate', '_sl_gate',
                  '_apply_record_gate', 'apply_record_gate'}
    assert called & gate_calls, f"{fn}() imports the gate but never calls it"


@pytest.mark.parametrize('knob,tier,probe', [
    ('MAP_ANON_COORD_DP', 'anon', '1'),
    ('MAP_FREE_COORD_DP', 'free', '5'),
])
def test_module_reads_the_maps_own_env_knobs(monkeypatch, knob, tier, probe):
    """One ladder, two readers — asserted by BEHAVIOUR, not by substring.

    facility_tier_gate reads these through a variable (`os.environ.get(name)`),
    so grepping it for the literal would fail while the code was correct, and
    would pass if someone left the name in a comment. Setting the variable and
    watching the answer move is the only assertion that means anything here.
    """
    monkeypatch.setenv(knob, probe)
    assert coord_dp_for_tier(tier) == int(probe), (
        f"{knob} is not the knob {tier} coordinates are read from")


def test_main_still_reads_the_same_two_knobs():
    """The map half of the contract. If main.py renames its knob, the corpus
    and the map can disagree about what 'anonymous' means again — which is the
    entire defect this module was extracted to remove.

    Since 2026-09-21 the map reads MAP_ANON_COORD_DP itself (its anonymous rung
    keeps the map's own 3dp default) and takes every other rung from
    coord_dp_for_tier — so MAP_FREE_COORD_DP has exactly ONE reader, this
    module, and the map cannot drift from the records on the free rung."""
    main_src = (ROOT / 'main.py').read_text(encoding='utf-8')
    assert "environ.get('MAP_ANON_COORD_DP'" in main_src, (
        "main.py no longer reads MAP_ANON_COORD_DP from the environment")
    fn = _func(_main_tree(), 'api_v1_map')
    imported = {(n.module, a.name) for n in ast.walk(fn)
                if isinstance(n, ast.ImportFrom) for a in n.names}
    assert ('util.facility_tier_gate', 'coord_dp_for_tier') in imported, (
        "the map no longer takes its rungs from coord_dp_for_tier")
    assert "environ.get('MAP_FREE_COORD_DP'" not in (ast.get_source_segment(main_src, fn) or ''), (
        "the map reads its own copy of the free rung again")


def test_the_free_rung_survives_an_unset_environment(monkeypatch):
    """The ladder must not depend on a Railway variable being present.

    Both defaults used to be 3, so with no env vars set anon and free were
    IDENTICAL by accident and signing up bought nothing. Since 2026-09-21 they
    are identical ON PURPOSE — 2 dp, ~1.1 km, for a record — and the rung a
    free account climbs is the monthly exact-location allowance. Its default
    lives in code too, so with nothing set: free rounds exactly like
    anonymous, never sharper, and still has an allowance to spend.
    """
    monkeypatch.delenv('MAP_ANON_COORD_DP', raising=False)
    monkeypatch.delenv('MAP_FREE_COORD_DP', raising=False)
    monkeypatch.delenv('FREE_EXACT_LOCATIONS_PER_MONTH', raising=False)
    assert coord_dp_for_tier('anon') == coord_dp_for_tier('free') == 2, (
        "with no environment set, a free record must round like an anonymous one")
    from util.location_meter import monthly_limit
    assert monthly_limit() == 10, (
        "with no environment set, a free account must still have an allowance")


def test_no_handler_in_the_class_is_left_ungated():
    """Anti-recurrence: a NEW route that SELECTs power_mw must be gated too.

    'Gating the map did not gate the corpus' is the recorded failure of the
    0801 fix. This fails when someone adds the fifth route.
    """
    # Verified live 2026-09-19, anonymous, no key and no cookie. An exemption
    # here is a MEASUREMENT, not an opinion: re-probe before adding to it.
    EXEMPT = {
        # Self-gated: carries its own tier resolution and field mask; since
        # 2026-09-21 its coordinate rungs come from coord_dp_for_tier (pinned by
        # test_main_still_reads_the_same_two_knobs and
        # tests/test_location_policy_every_surface.py).
        # Probed: tier=anonymous, _coord_precision_dp=2, no power_mw, no provider.
        'api_v1_map',
        # Probed: HTTP 402 for anonymous callers.
        'api_site_score',
        # Freemium by design and NOT in this class: publishes only 100km-radius
        # AGGREGATES (capacity_mw_100km, facilities_100km, substations_50km).
        # No single facility's power_mw or coordinates are recoverable from it;
        # the 2030-2050 forecast is Pro-gated. Probed: HTTP 200, aggregates only.
        'api_site_forecast',
        # Admin maintenance. Probed: HTTP 401 for anonymous callers, both.
        '_admin_dedup_drain',
        '_admin_dedup_facilities_soft',
    }
    tree = _main_tree()
    main_src = (ROOT / 'main.py').read_text(encoding='utf-8')
    marker_lines = _marker_lines(main_src)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if not _spans_all(node, marker_lines):
            continue
        src = ast.get_source_segment(main_src, node) or ''
        # A handler is in the class if it SELECTs power_mw alongside coordinates
        # out of the registry table.
        sql_like = 'discovered_facilities' in src and 'power_mw' in src
        if not sql_like:
            continue
        if 'latitude' not in src:
            continue
        mods = {n.module for n in ast.walk(node) if isinstance(n, ast.ImportFrom)}
        calls = set()
        for n in ast.walk(node):
            if isinstance(n, ast.Call):
                f = n.func
                if isinstance(f, ast.Name):
                    calls.add(f.id)
                elif isinstance(f, ast.Attribute):
                    calls.add(f.attr)
        # An import alone is not a gate: the fail-closed `except` branch of a
        # gated handler imports the module too, so importing survives having the
        # happy-path call deleted. Require a call as well.
        GATE_CALLS = {'gate_record', 'gate_records', 'coarsen_coords_deep',
                      'get_request_tier', 'caller_is_privileged',
                      'detect_tier_for_data_gate',
                      '_fg_rec', '_fg_rows', '_sc_deep', '_fid_gate', '_sl_gate',
                      '_grt_fac', '_sl_tier', '_fg_tier', '_fid_tier',
                      # r-slashparity (2026-09-20): the shared single-record
                      # envelope. Calling it IS the gate.
                      '_apply_record_gate', 'apply_record_gate',
                      '_fg_tier_of_request'}
        # A handler that calls the shared envelope reaches the module through
        # it and need not import the module itself — requiring the import
        # inside the handler is what would push the policy back into the routes.
        _via_shared = bool(calls & {'_apply_record_gate', 'apply_record_gate'})
        gated = _via_shared or bool(
            (mods & {'util.facility_tier_gate', 'api_tier_gating',
                     'routes.tier_gate'})
            and (calls & GATE_CALLS))
        if not gated and node.name not in EXEMPT:
            offenders.append(node.name)
    assert offenders == [], (
        "these handlers read power_mw + coordinates out of "
        "discovered_facilities with no tier gate in their path: "
        f"{sorted(offenders)}")


def test_every_returned_record_in_a_class_handler_is_gated():
    """Branch-level companion to test_no_handler_in_the_class_is_left_ungated.

    That test asks whether the FUNCTION imports and calls a gate. A function
    with two exits needs only one of them to satisfy it. `facility_by_slug` is
    exactly that shape — #4856 gated its hash-slug branch (`_resp_slug`) and
    left its numeric-id branch (`_resp_id`) returning the full row, and the
    function-level check stayed green the whole time because the slug branch's
    import and call were enough to answer for both.

    Measured live 2026-09-19 after #4856 shipped, anonymous, cache-busted:

        /api/v1/facilities/<id>   ->  8 fields, tier=free, _upgrade present
        /api/v1/facilities/<id>/  -> 14 fields, power_mw + coordinates + address

    One trailing slash chose the branch. So the unit of this check is the
    RESPONSE, not the function: every response variable a class handler hands
    to jsonify must itself have been through the gate.
    """
    src = (ROOT / 'main.py').read_text(encoding='utf-8')
    tree = _main_tree()

    # Names a gated response carries. Assigning any of them marks that
    # particular response object as having been through the gate.
    GATE_MARKS = {'_gated', 'data'}
    GATE_CALLS = {'gate_record', 'gate_records', '_fg_rec', '_fg_rows',
                  'coarsen_coords_deep', '_sc_deep', '_fid_gate', '_sl_gate',
                  '_apply_record_gate', 'apply_record_gate'}
    # r-slashparity (2026-09-20): a response can now be gated IN PLACE —
    # `_apply_record_gate(_resp_id, tier)` mutates and returns nothing. That is
    # a bare Expr, not an Assign, so the re-keying rule below cannot see it.
    # Passing the response INTO a gate call marks it gated too.
    IN_PLACE_GATES = {'_apply_record_gate', 'apply_record_gate'}

    offenders = []
    marker_lines = _marker_lines(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if not _spans_all(node, marker_lines):
            continue
        fsrc = ast.get_source_segment(src, node) or ''
        if not ('discovered_facilities' in fsrc and 'power_mw' in fsrc
                and 'latitude' in fsrc):
            continue
        if node.name in ('api_v1_map', 'api_site_score', 'api_site_forecast',
                         '_admin_dedup_drain', '_admin_dedup_facilities_soft'):
            continue

        # Response variables that reach the client as a record.
        returned = set()
        for n in ast.walk(node):
            if (isinstance(n, ast.Return) and isinstance(n.value, ast.Call)
                    and isinstance(n.value.func, ast.Name)
                    and n.value.func.id == 'jsonify'
                    and n.value.args
                    and isinstance(n.value.args[0], ast.Name)):
                returned.add(n.value.args[0].id)

        # Response variables that were re-keyed after a gate call ran.
        gated = set()
        for n in ast.walk(node):
            if not isinstance(n, ast.Assign):
                continue
            called = {c.func.id for c in ast.walk(n)
                      if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
            for t in n.targets:
                if (isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name)
                        and isinstance(t.slice, ast.Constant)
                        and t.slice.value in GATE_MARKS):
                    if t.slice.value == '_gated' or (called & GATE_CALLS):
                        gated.add(t.value.id)
        for n in ast.walk(node):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id in IN_PLACE_GATES):
                for a in n.args:
                    if isinstance(a, ast.Name):
                        gated.add(a.id)

        for var in sorted(returned - gated):
            offenders.append(f"{node.name}:{var}")

    assert offenders == [], (
        "these response objects are returned from a class handler without "
        "passing through the tier gate — a sibling branch being gated does "
        f"not cover them: {sorted(offenders)}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# NUMERIC coordinates. psycopg2 returns a NUMERIC column as decimal.Decimal,
# and _round_coords used to round only int/float — so a Decimal latitude
# passed every gate at full precision and reported nothing rounded.
# ─────────────────────────────────────────────────────────────────────────────

def test_decimal_coordinates_are_rounded_like_floats():
    from decimal import Decimal
    rec = dict(RICH, latitude=Decimal('12.345678'), longitude=Decimal('-45.678912'))
    out, n = gate_record(rec, 'anon')
    dp = coord_dp_for_tier('anon')
    assert out['latitude'] == round(12.345678, dp) and isinstance(out['latitude'], float)
    assert out['longitude'] == round(-45.678912, dp) and isinstance(out['longitude'], float)
    assert out['coordinates_status'] == f'approximate_{dp}dp'
    assert n > 0


def test_an_already_coarse_decimal_is_not_counted_as_a_redaction():
    """Decimal('12.35') != 12.35 exactly; comparing that way would inflate the
    tally and relabel an untouched coordinate as approximate."""
    from decimal import Decimal
    thin = {'name': 'X', 'city': 'Y', 'state': 'Z', 'country': 'XX',
            'status': 'Operational', 'slug': 's',
            'latitude': Decimal('12.35'), 'longitude': Decimal('-45.68')}
    out, n = gate_record(thin, 'anon')
    assert n == 0 and 'coordinates_status' not in out
    assert out['latitude'] == 12.35 and isinstance(out['latitude'], float)


def test_decimal_coordinates_are_reached_by_the_deep_coarsener_too():
    from decimal import Decimal
    payload = {'data': [{'lat': Decimal('1.234567'), 'lng': Decimal('2.345678')}]}
    assert coarsen_coords_deep(payload, 'anon') == 2
    assert payload['data'][0]['lat'] == round(1.234567, coord_dp_for_tier('anon'))
