"""Guard: /api/v1/infrastructure/stats must not merge facilities into an
asset total, and must not publish 0 for an unmeasured member.

FENCES the 2026-07-29 decontamination of
routes/infrastructure_data_routes.py::get_infrastructure_stats. What the
endpoint published live before the fix:

    stats.discovered_facilities  23094
    stats.transmission_lines     56108   <- WRONG TABLE (transmission_lines_eia)
    stats.submarine_cables           0   <- table exists, never populated
    stats.submarine_cable_landings   0   <- same
    total                       305471   <- sum(stats.values()), so it INCLUDED
                                            the 23,094 data-centre facilities

Three house rules were broken at once:
  * facilities and infrastructure assets are separate populations and must
    never be summed;
  * UNMEASURED emits null + a reason, never 0;
  * a published figure states its basis.

These are BEHAVIOUR assertions, not source greps. Every test below drives
the real shipped function `build_infrastructure_stats_payload` with a
synthetic `measured` map, so a comment or a renamed variable cannot
satisfy them — only the actual arithmetic and the actual null-handling
can. (A grep-style fence was considered and rejected: comments satisfy
greps.)

No DB and no network — the payload builder is pure by construction, which
is why it was split out of the route. Nothing runs at module scope.

Run locally:
    python3 -m pytest tests/test_infra_stats_asset_total.py -v
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# The live figures probed on dchub.cloud 2026-07-29, used so a regression
# reproduces the exact contamination this fence was written for rather
# than some tidy invented case.
LIVE_FACILITIES = 23094
LIVE_SUBSTATIONS = 126841
LIVE_TRANSMISSION_MAINTAINED = 94626   # public.transmission_lines
LIVE_TRANSMISSION_SNAPSHOT = 56108     # public.transmission_lines_eia
LIVE_GAS = 30918
LIVE_FIBER = 55064
LIVE_PLANTS = 13446

# 126841 + 94626 + 30918 + 55064 + 13446
EXPECTED_ASSET_TOTAL = 320895
# ...plus the 23,094 facilities the deprecated `total` still merges.
EXPECTED_LEGACY_TOTAL = EXPECTED_ASSET_TOTAL + LIVE_FACILITIES


def _mod():
    """Import the route module. Fails loudly if it can't be imported.

    An ImportError here must NOT be swallowed into a skip: a fence that
    silently skips is a fence that is not running.
    """
    import routes.infrastructure_data_routes as m
    return m


def _live_measured():
    """The live shape: five asset layers measured, both subsea unmeasured."""
    return {
        'gas_pipelines': (LIVE_GAS, None),
        'power_plants': (LIVE_PLANTS, None),
        'transmission_lines': (LIVE_TRANSMISSION_MAINTAINED, None),
        'submarine_cables': (None, 'table exists but holds 0 rows: ingest never ran'),
        'submarine_cable_landings': (None, 'table exists but holds 0 rows: ingest never ran'),
        'substations': (LIVE_SUBSTATIONS, None),
        'fiber_routes': (LIVE_FIBER, None),
        'transmission_lines_geocoded_snapshot': (LIVE_TRANSMISSION_SNAPSHOT, None),
        'discovered_facilities': (LIVE_FACILITIES, None),
    }


def test_payload_builder_exists_and_is_callable():
    """The endpoint's arithmetic must be reachable without a DB.

    If this fails, every other test below is vacuous — so it is asserted
    separately rather than left implicit in the other tests' imports.
    """
    m = _mod()
    assert hasattr(m, 'build_infrastructure_stats_payload'), (
        'build_infrastructure_stats_payload is missing — the asset total is not '
        'testable without a database, so it is not fenced.')
    assert callable(m.build_infrastructure_stats_payload)


def test_asset_total_excludes_facilities():
    """★ THE CORE ASSERTION. Fails on pre-fix code, which summed facilities."""
    m = _mod()
    out = m.build_infrastructure_stats_payload(_live_measured())

    total = out.get('infrastructure_assets_total')
    assert total == EXPECTED_ASSET_TOTAL, (
        'infrastructure_assets_total should be %d (the five measured asset '
        'layers) but was %r' % (EXPECTED_ASSET_TOTAL, total))

    # The load-bearing property, stated independently of the constant
    # above so that bumping a live figure can't quietly re-admit
    # facilities: the asset total must be strictly less than a total
    # containing facilities, by exactly the facility count.
    assert total + LIVE_FACILITIES == out['total'], (
        'the deprecated `total` must exceed the asset total by exactly the '
        'facility count — otherwise one of the two is mixing populations')
    assert out['stats']['discovered_facilities'] == LIVE_FACILITIES, (
        'discovered_facilities must still be PUBLISHED (this fix is additive); '
        'it just must not be summed into an asset total')


def test_facilities_and_subset_are_named_as_excluded_with_a_reason():
    """Excluding a member silently is indistinguishable from forgetting it."""
    m = _mod()
    out = m.build_infrastructure_stats_payload(_live_measured())
    basis = out.get('infrastructure_assets_basis')
    assert isinstance(basis, dict), 'the asset total must publish its basis'

    assert 'discovered_facilities' not in basis['members_summed']
    assert 'transmission_lines_geocoded_snapshot' not in basis['members_summed'], (
        'the geocoded snapshot is a SUBSET of transmission_lines; summing both '
        'double-counts transmission')

    excluded = basis.get('excluded') or {}
    for key in ('discovered_facilities', 'transmission_lines_geocoded_snapshot'):
        assert key in excluded, '%s must be named as excluded' % key
        assert excluded[key] and len(excluded[key]) > 30, (
            '%s must say WHY it is excluded, not just that it is' % key)

    # The member list must actually reconstruct the number it claims.
    rebuilt = sum(out['stats'][k] for k in basis['members_summed'])
    assert rebuilt == out['infrastructure_assets_total'], (
        'members_summed must reproduce infrastructure_assets_total exactly, '
        'or the published basis is decorative')


def test_unmeasured_member_is_null_with_a_reason_never_zero():
    """★ Fails on pre-fix code, which published 0 for both subsea members."""
    m = _mod()
    out = m.build_infrastructure_stats_payload(_live_measured())

    for key in ('submarine_cables', 'submarine_cable_landings'):
        assert out['stats'][key] is None, (
            '%s is unmeasured (table exists, 0 rows, ingest never ran) and must '
            'be null — publishing 0 asserts the population IS zero, which is '
            'false: upstream is ~717 cables / ~1,918 landings' % key)
        assert out['stats'][key] != 0
        reason = (out.get('unmeasured') or {}).get(key)
        assert reason and len(reason) > 20, (
            '%s must carry a reason explaining why it is unmeasured' % key)

    basis = out['infrastructure_assets_basis']
    assert basis['complete'] is False, (
        'two asset layers are unmeasured, so the total cannot claim to be complete')
    assert basis['is_floor'] is True, (
        'an incomplete total must be labelled a floor — it can only be below '
        'reality, never above it')
    assert sorted(basis['members_unmeasured']) == [
        'submarine_cable_landings', 'submarine_cables']


def test_unmeasured_member_contributes_nothing_to_any_total():
    """A null must not be coerced to 0 and quietly added, nor crash the sum."""
    m = _mod()
    measured = _live_measured()
    baseline = m.build_infrastructure_stats_payload(measured)

    # Knock out a measured layer; both totals must drop by exactly its value.
    measured['substations'] = (None, 'table_absent: public.substations does not exist')
    out = m.build_infrastructure_stats_payload(measured)

    assert out['stats']['substations'] is None
    assert out['infrastructure_assets_total'] == (
        baseline['infrastructure_assets_total'] - LIVE_SUBSTATIONS)
    assert out['total'] == baseline['total'] - LIVE_SUBSTATIONS
    assert 'substations' in out['infrastructure_assets_basis']['members_unmeasured']


def test_all_asset_layers_unmeasured_yields_null_not_zero():
    """0 assets would read as "there is no infrastructure". Must be null."""
    m = _mod()
    measured = {k: (None, 'table_absent') for k, _t, _r in m._STATS_MEMBERS}
    out = m.build_infrastructure_stats_payload(measured)

    assert out['infrastructure_assets_total'] is None, (
        'with nothing measured the total must be null, never 0')
    assert 'unavailable_reason' in out['infrastructure_assets_basis']


def test_transmission_counts_the_maintained_table_not_the_snapshot():
    """★ Fails on pre-fix code, where transmission_lines -> transmission_lines_eia.

    The two endpoints disagreed 1.7x (94,626 vs 56,108) on one concept
    because they counted two different tables with identical unfiltered
    COUNT(*). The maintained table wins the name; the snapshot gets its own.
    """
    m = _mod()
    by_key = {k: (table, role) for k, table, role in m._STATS_MEMBERS}

    assert by_key['transmission_lines'][0] == 'transmission_lines', (
        'the member published as `transmission_lines` must count the MAINTAINED '
        'public.transmission_lines table (refreshed by '
        'routes/transmission_ingest.py, reported fresh by the freshness radar '
        'and by /api/v1/stats), not transmission_lines_eia, which has no writer '
        'anywhere in this repo and therefore no refresh path')
    assert by_key['transmission_lines'][1] == 'asset'

    snap = by_key.get('transmission_lines_geocoded_snapshot')
    assert snap is not None, (
        'the stale geocoded table must keep a NAME of its own rather than being '
        'dropped, so a consumer that sees the number move can tell which '
        'population it had been reading')
    assert snap[0] == 'transmission_lines_eia'
    assert snap[1] == 'subset', 'the snapshot must never sum into the asset total'

    # Exactly one member may claim the plain `transmission_lines` name.
    assert [k for k, _t, _r in m._STATS_MEMBERS
            if k == 'transmission_lines'] == ['transmission_lines']


def test_no_asset_member_counts_a_facility_table():
    """Structural: nothing tagged 'asset' may point at a facility table."""
    m = _mod()
    facility_tables = {'discovered_facilities', 'facilities', 'data_centers'}
    for key, table, role in m._STATS_MEMBERS:
        if role == 'asset':
            assert table not in facility_tables, (
                'asset member %r counts facility table %r — facilities and '
                'infrastructure assets are separate populations' % (key, table))


def test_member_table_is_an_explicit_allow_list_not_an_open_sum():
    """A blind sum(stats.values()) lets any added key move a published figure."""
    m = _mod()
    assert isinstance(m._STATS_MEMBERS, tuple), (
        '_STATS_MEMBERS must be an immutable allow-list')
    for entry in m._STATS_MEMBERS:
        assert len(entry) == 3, (
            'every member must carry an explicit role tag, so adding one cannot '
            'default into the asset total: %r' % (entry,))
        assert entry[2] in ('asset', 'subset', 'facility')

    # An unrecognised key handed in must NOT reach any total.
    measured = _live_measured()
    measured['some_new_layer_someone_added'] = (999999, None)
    out = m.build_infrastructure_stats_payload(measured)
    assert out['infrastructure_assets_total'] == EXPECTED_ASSET_TOTAL
    assert out['total'] == EXPECTED_LEGACY_TOTAL
    assert 'some_new_layer_someone_added' not in out['stats']


def test_deprecated_total_is_preserved_and_labelled():
    """Additive change: `total` keeps its composition, but says what it merges."""
    m = _mod()
    out = m.build_infrastructure_stats_payload(_live_measured())
    assert out['total'] == EXPECTED_LEGACY_TOTAL, (
        '`total` is public; unknown consumers read it. It must keep its '
        'historical composition (assets + facilities) rather than being '
        'redefined underneath them.')
    note = out.get('total_note') or ''
    assert 'DEPRECATED' in note
    assert 'infrastructure_assets_total' in note, (
        'the deprecation note must point at the correctly-scoped field')


def test_zero_row_count_becomes_unmeasured_in_the_real_measure_helper():
    """The 0 -> null rule must live in the shipped measure path, not just here.

    Drives the real _measure_member against a fake cursor. The old code
    was `except: stats[key] = 0`, which made a missing table, a
    permissions error and an empty table indistinguishable.
    """
    m = _mod()

    class FakeCur:
        def __init__(self, regclass, count):
            self._regclass = regclass
            self._count = count
            self._last = None

        def execute(self, sql, params=None):
            self._last = 'regclass' if 'to_regclass' in sql else 'count'
            if self._last == 'count' and isinstance(self._count, Exception):
                raise self._count

        def fetchone(self):
            if self._last == 'regclass':
                return (self._regclass,)
            return (self._count,)

    class FakeConn:
        def rollback(self):
            pass

    conn = FakeConn()

    # Table exists, 0 rows -> unmeasured with the member's own reason.
    val, reason = m._measure_member(
        FakeCur('public.submarine_cables', 0), conn,
        'submarine_cables', 'submarine_cables')
    assert val is None, 'a 0 row count must not be published as a figure'
    assert 'ingest' in reason.lower() or '717' in reason

    # Table absent -> unmeasured, and distinguishable from empty.
    val, reason = m._measure_member(
        FakeCur(None, 0), conn, 'substations', 'substations')
    assert val is None
    assert 'absent' in reason.lower(), (
        'a missing table must report table_absent, not the empty-table reason — '
        'they are different claims')

    # Query error -> unmeasured, NOT 0.
    val, reason = m._measure_member(
        FakeCur('public.substations', RuntimeError('boom')), conn,
        'substations', 'substations')
    assert val is None, 'a failed count must never publish 0'
    assert 'failed' in reason.lower()

    # Healthy count passes through unchanged.
    val, reason = m._measure_member(
        FakeCur('public.substations', LIVE_SUBSTATIONS), conn,
        'substations', 'substations')
    assert val == LIVE_SUBSTATIONS
    assert reason is None


@pytest.mark.xfail(strict=True, reason=(
    'MUST-FAIL control — deliberately asserts a falsehood. strict=True means '
    'pytest reports it as xfailed, and would report a hard FAILURE if it ever '
    'started passing. Its job is to prove this file is really being collected '
    'and executed: a suite that exits rc0 having run zero tests is silent '
    'green, and this repo has shipped that twice. If the xfail count for this '
    'file drops to 0, the fence is not running.'))
def test_must_fail_control_proves_this_suite_actually_runs():
    assert False, 'control: this assertion is supposed to fail'
