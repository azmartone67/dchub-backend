"""Guard: the subsea surfaces must read the POPULATED tables, must not publish
a route-segment count as a cable count, and must not publish an invented cause.

FENCES the 2026-07-29 second-pass correction of the subsea layer. Three
distinct defects, all live-probed on dchub.cloud and against the live Neon
replica before this change.

──────────────────────────────────────────────────────────────────────────
1. WRONG TABLE. /api/v1/infrastructure/stats counted `submarine_cables` and
   `submarine_cable_landings`. Both tables EXIST and hold 0 rows (to_regclass
   resolves, COUNT(*) = 0). They are ABANDONED: the sole writer for the first
   is the standalone ETL 01_submarine_cables.py, whose filename begins with a
   digit so it can never be imported as a module and which is scheduled
   nowhere; the second has no CREATE and no writer at all.
   The POPULATED pair was in the same database the whole time —
   subsea_cables (691 rows) and subsea_landing_points (1,908 rows), written by
   subsea_cable_ingestion.py and already served at /api/v1/subsea/*.
   Live before:  infrastructure_assets_total 320,895, is_floor true,
                 members_unmeasured [submarine_cables,
                 submarine_cable_landings]
   Expected after: 323,494, is_floor false, members_unmeasured []

2. FALSE PUBLISHED CAUSE. The `unmeasured` reason shipped to customers read
   "the subsea ingest has never run (subsea_cable_ingestion is registered in
   main.py under entry-point names it does not define, so it fires nothing)".
   Every clause was false: the ingest ran on 2026-03-27, it populated, and the
   two bad registrations are manual internal-key admin endpoints rather than
   the trigger. A confidently-wrong explanation shipped to a customer is worse
   than no explanation, so the reason text is fenced here as the published
   claim it is.

3. SEGMENT COUNT PUBLISHED AS A CABLE COUNT. /api/v1/infrastructure/
   submarine-cables published counts.cables = len(features) = 717. Measured on
   the live upstream payload: 717 features, 696 distinct properties.id, 696
   distinct names, 20 ids carrying more than one feature (echo = 3), every
   geometry a MultiLineString. Same class as the documented hosting_capacity
   "rows = GIS vertices" trap.

──────────────────────────────────────────────────────────────────────────
These are BEHAVIOUR assertions. Every test drives a real shipped function —
build_infrastructure_stats_payload, _measure_member, build_counts,
_distinct_feature_ids — or the real Flask view against a fake cursor. A comment
or a renamed variable cannot satisfy them. The two tests that do assert on
published TEXT (the cause and the basis) are asserting the API RESPONSE BODY,
which is the deliverable itself, not source commentary.

No DB and no network. Nothing runs at module scope.

Run locally:
    python3 -m pytest tests/test_subsea_wrong_table_and_segment_counts.py -v
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Live figures, measured 2026-07-29 on the Neon read replica and on the live
# TeleGeography payload. Used so a regression reproduces the real numbers.
LIVE_SUBSEA_CABLES = 691           # public.subsea_cables
LIVE_SUBSEA_LANDING_POINTS = 1908  # public.subsea_landing_points
UPSTREAM_CABLE_FEATURES = 717      # cable-geo.json len(features)
UPSTREAM_CABLE_DISTINCT = 696      # distinct properties.id
UPSTREAM_LANDING_FEATURES = 1918

LIVE_FACILITIES = 23094
LIVE_SUBSTATIONS = 126841
LIVE_TRANSMISSION_MAINTAINED = 94626
LIVE_TRANSMISSION_SNAPSHOT = 56108
LIVE_GAS = 30918
LIVE_FIBER = 55064
LIVE_PLANTS = 13446

# What the endpoint published live BEFORE this change (both subsea unmeasured).
ASSET_TOTAL_BEFORE = 320895
# ...and after, with the two repointed members measured.
ASSET_TOTAL_AFTER = (ASSET_TOTAL_BEFORE
                     + LIVE_SUBSEA_CABLES + LIVE_SUBSEA_LANDING_POINTS)

# The abandoned tables. No 'asset' member may ever point at one again.
ABANDONED_TABLES = {'submarine_cables', 'submarine_cable_landings'}

# Fragments of the disproved causal story. None may reappear in a published
# reason. Kept as separate fragments so a reworded version of the same false
# claim is still caught.
DISPROVED_CAUSE_FRAGMENTS = (
    'has never run',
    'never been run',
    'fires nothing',
    'entry-point names it does not define',
    'entry point names it does not define',
    'no callable entry point',
)


def _mod():
    """Import the stats route module. An ImportError must NOT become a skip."""
    import routes.infrastructure_data_routes as m
    return m


def _proxy_mod():
    """Import the TeleGeography proxy module."""
    import routes.submarine_cables as m
    return m


def _measured_all_present():
    """Every asset layer measured, including the two repointed subsea members."""
    return {
        'gas_pipelines': (LIVE_GAS, None),
        'power_plants': (LIVE_PLANTS, None),
        'transmission_lines': (LIVE_TRANSMISSION_MAINTAINED, None),
        'submarine_cables': (LIVE_SUBSEA_CABLES, None),
        'submarine_cable_landings': (LIVE_SUBSEA_LANDING_POINTS, None),
        'substations': (LIVE_SUBSTATIONS, None),
        'fiber_routes': (LIVE_FIBER, None),
        'transmission_lines_geocoded_snapshot': (LIVE_TRANSMISSION_SNAPSHOT, None),
        'discovered_facilities': (LIVE_FACILITIES, None),
    }


# ══════════════════════════════════════════════════════════════════════
# 1. THE WRONG-TABLE REPOINT
# ══════════════════════════════════════════════════════════════════════

def test_subsea_members_count_the_populated_tables():
    """★ CORE. Fails on pre-fix code, where both members read 0-row tables."""
    m = _mod()
    by_key = {k: (table, role) for k, table, role in m._STATS_MEMBERS}

    assert by_key['submarine_cables'][0] == 'subsea_cables', (
        'the member published as `submarine_cables` must count '
        'public.subsea_cables (691 rows, written by subsea_cable_ingestion.py '
        'ON CONFLICT (cable_id), already served at /api/v1/subsea/cables), not '
        'public.submarine_cables, which exists, holds 0 rows, and whose only '
        'writer in the whole repo is the standalone ETL 01_submarine_cables.py '
        '— a filename beginning with a digit, so it can never be imported as a '
        'module, and it is scheduled nowhere')
    assert by_key['submarine_cable_landings'][0] == 'subsea_landing_points', (
        'the member published as `submarine_cable_landings` must count '
        'public.subsea_landing_points (1,908 rows), not '
        'public.submarine_cable_landings, which has no CREATE and no writer '
        'anywhere in the repo')

    assert by_key['submarine_cables'][1] == 'asset'
    assert by_key['submarine_cable_landings'][1] == 'asset'


def test_no_member_reads_an_abandoned_zero_row_table():
    """Structural: the abandoned pair must not be counted under ANY key.

    Stated independently of the two assertions above so that re-adding one
    of them under a new published key is also caught.
    """
    m = _mod()
    for key, table, role in m._STATS_MEMBERS:
        assert table not in ABANDONED_TABLES, (
            'member %r counts %r, an abandoned 0-row table with no working '
            'writer. Role was %r. Use subsea_cables / subsea_landing_points.'
            % (key, table, role))


def test_published_keys_did_not_change():
    """The repoint fixes the TABLE, not the customer-facing key.

    `submarine_cables` and `submarine_cable_landings` name the CONCEPT and
    unknown consumers already read them. Renaming them alongside a value
    change would break consumers and hide the correction in the same move.
    """
    m = _mod()
    keys = [k for k, _t, _r in m._STATS_MEMBERS]
    assert 'submarine_cables' in keys
    assert 'submarine_cable_landings' in keys
    # And each concept is claimed exactly once.
    assert keys.count('submarine_cables') == 1
    assert keys.count('submarine_cable_landings') == 1


def test_asset_total_moves_by_exactly_the_two_repointed_layers():
    """★ The published figure must move 320,895 -> 323,494, and by that much.

    Asserted both against the constant and as a relationship, so bumping a
    live figure cannot quietly change what the move consists of.
    """
    m = _mod()
    after = m.build_infrastructure_stats_payload(_measured_all_present())

    assert after['infrastructure_assets_total'] == ASSET_TOTAL_AFTER, (
        'expected %d (the five previously-measured layers plus 691 cables and '
        '1,908 landing points) but got %r'
        % (ASSET_TOTAL_AFTER, after['infrastructure_assets_total']))

    # Same payload builder, both subsea members unmeasured = the live-before
    # shape. The delta must be exactly the two layers, nothing else.
    before_measured = _measured_all_present()
    before_measured['submarine_cables'] = (None, 'table_absent')
    before_measured['submarine_cable_landings'] = (None, 'table_absent')
    before = m.build_infrastructure_stats_payload(before_measured)

    assert before['infrastructure_assets_total'] == ASSET_TOTAL_BEFORE
    assert (after['infrastructure_assets_total']
            - before['infrastructure_assets_total']
            == LIVE_SUBSEA_CABLES + LIVE_SUBSEA_LANDING_POINTS)

    # members_summed must still reconstruct the number it claims.
    basis = after['infrastructure_assets_basis']
    rebuilt = sum(after['stats'][k] for k in basis['members_summed'])
    assert rebuilt == after['infrastructure_assets_total'], (
        'members_summed must reproduce infrastructure_assets_total exactly, or '
        'the published basis is decorative')


def test_completeness_flips_and_the_floor_label_is_dropped():
    """With every asset member measured, `is_floor` must stop claiming a floor."""
    m = _mod()
    out = m.build_infrastructure_stats_payload(_measured_all_present())
    basis = out['infrastructure_assets_basis']

    assert basis['members_unmeasured'] == [], (
        'no asset member should remain unmeasured: %r' % (basis['members_unmeasured'],))
    assert basis['complete'] is True
    assert basis['is_floor'] is False, (
        'with every member measured the total is no longer a floor')
    assert out['stats']['submarine_cables'] == LIVE_SUBSEA_CABLES
    assert out['stats']['submarine_cable_landings'] == LIVE_SUBSEA_LANDING_POINTS
    assert not (out.get('unmeasured') or {}), (
        'nothing should be reported unmeasured when everything measured')


def test_complete_does_not_silently_claim_liveness():
    """`complete: true` must not read as "every member is a live bind".

    The subsea members are a dated snapshot with a measured two-way drift and
    NO delete path, so the total is neither live nor a strict floor. That has
    to be visible in the payload, not just true in a comment.
    """
    m = _mod()
    out = m.build_infrastructure_stats_payload(_measured_all_present())
    basis = out['infrastructure_assets_basis']

    assert 'complete_means' in basis, (
        'complete=true must say what it means; a bare boolean invites '
        '"everything is current"')
    assert 'measured' in basis['complete_means'].lower()

    mb = basis.get('member_basis') or {}
    for key in ('submarine_cables', 'submarine_cable_landings'):
        assert key in mb, (
            '%s is a dated snapshot and must publish its basis' % key)
        entry = mb[key]
        assert entry.get('is_live_bind') is False, (
            '%s is a 2026-03-27 snapshot, not a live bind' % key)
        assert entry.get('as_of'), '%s must state its vintage' % key
        assert entry.get('as_of_basis'), (
            '%s must state HOW its vintage was determined, not just assert one'
            % key)
        # The unit is the whole point of the repoint: prove it is stated.
        unit = (entry.get('unit') or '').lower()
        assert 'distinct' in unit, (
            '%s must state that a row is one DISTINCT upstream id' % key)
        # Drift direction must be published — a snapshot that only ever
        # rounded down would be a floor, and this one does not.
        assert entry.get('drift'), '%s must state its known drift' % key
        assert 'not a floor' in entry['drift'].lower(), (
            '%s upserts and never deletes, so it can hold rows that no longer '
            'exist upstream — it must not be presented as a floor' % key)


def test_member_basis_is_not_published_for_an_unmeasured_member():
    """A basis describing a figure that is null would be a claim about nothing."""
    m = _mod()
    measured = _measured_all_present()
    measured['submarine_cables'] = (None, 'table_absent: gone')
    out = m.build_infrastructure_stats_payload(measured)

    mb = out['infrastructure_assets_basis'].get('member_basis') or {}
    assert 'submarine_cables' not in mb, (
        'submarine_cables is unmeasured here; publishing a vintage for it '
        'would describe a figure that is not in the payload')
    assert 'submarine_cable_landings' in mb, (
        'the other member is still measured and must keep its basis')


# ══════════════════════════════════════════════════════════════════════
# 2. THE FALSE PUBLISHED CAUSE
# ══════════════════════════════════════════════════════════════════════

def test_no_published_reason_repeats_the_disproved_cause():
    """★ Fails on pre-fix code, which shipped the invented mechanism.

    This asserts on published TEXT deliberately: the `unmeasured` reason IS
    the API response body, not source commentary. It was served live to
    keyless callers, and every clause of it was false.
    """
    m = _mod()
    reasons = dict(m._MEMBER_EMPTY_REASON)
    reasons['__default__'] = m._DEFAULT_EMPTY_REASON

    for key, text in reasons.items():
        low = (text or '').lower()
        for frag in DISPROVED_CAUSE_FRAGMENTS:
            assert frag not in low, (
                'the reason published for %r still contains %r. Measured '
                '2026-07-29: subsea_cable_ingestion HAS run (2026-03-27) and '
                'DID populate 691 cables / 1,908 landing points; the two bad '
                'main.py registrations are manual internal-key admin endpoints, '
                'not the trigger, and the real trigger POST '
                '/api/jobs/subsea-sync -> fiber_integration.run_subsea_sync was '
                'wired correctly all along.' % (key, frag))


def test_an_empty_subsea_table_reports_unverified_rather_than_a_mechanism():
    """A reason may report what was observed; it may not invent a cause.

    Drives the real _measure_member so the 0 -> null rule and the reason text
    are both exercised on the shipped path.
    """
    m = _mod()

    class FakeCur:
        def __init__(self, regclass, count):
            self._regclass, self._count, self._last = regclass, count, None

        def execute(self, sql, params=None):
            self._last = 'regclass' if 'to_regclass' in sql else 'count'

        def fetchone(self):
            return (self._regclass,) if self._last == 'regclass' else (self._count,)

    class FakeConn:
        def rollback(self):
            pass

    conn = FakeConn()

    for key, table in (('submarine_cables', 'subsea_cables'),
                       ('submarine_cable_landings', 'subsea_landing_points')):
        val, reason = m._measure_member(
            FakeCur('public.' + table, 0), conn, key, table)
        assert val is None, 'a 0 row count must never be published as a figure'
        assert reason, '%s must carry a reason' % key
        low = reason.lower()
        assert 'unverified' in low, (
            '%s: an empty table of a layer known to have held rows must say the '
            'CAUSE IS UNVERIFIED rather than assert one — that is the mistake '
            'this whole fence exists for. Got: %s' % (key, reason))
        assert table in reason, (
            '%s must name the table it actually read (%s), so a future reader '
            'can tell which population was measured' % (key, table))
        for frag in DISPROVED_CAUSE_FRAGMENTS:
            assert frag not in low

    # A healthy count still passes straight through.
    val, reason = m._measure_member(
        FakeCur('public.subsea_cables', LIVE_SUBSEA_CABLES), conn,
        'submarine_cables', 'subsea_cables')
    assert val == LIVE_SUBSEA_CABLES
    assert reason is None


# ══════════════════════════════════════════════════════════════════════
# 3. SEGMENT COUNT vs CABLE COUNT
# ══════════════════════════════════════════════════════════════════════

def _fake_cable_fc():
    """A FeatureCollection with the live upstream shape: more features than ids.

    Reproduces the measured 2026-07-29 pattern in miniature — `echo` arrives as
    3 MultiLineString features, `rising-8` as 2 — so a regression to
    len(features) is caught by arithmetic rather than by a string match.
    """
    feats = []
    for cid in ('echo', 'echo', 'echo', 'rising-8', 'rising-8'):
        feats.append({'properties': {'id': cid, 'name': cid.upper()},
                      'geometry': {'type': 'MultiLineString', 'coordinates': []}})
    for cid in ('2africa', 'aec-1', 'alba-1'):
        feats.append({'properties': {'id': cid, 'name': cid.upper()},
                      'geometry': {'type': 'MultiLineString', 'coordinates': []}})
    # 8 features, 5 distinct cables.
    return {'type': 'FeatureCollection', 'features': feats}


def _fake_landing_fc(n=4):
    return {'type': 'FeatureCollection', 'features': [
        {'properties': {'id': 'lp-%d' % i, 'name': 'LP %d' % i},
         'geometry': {'type': 'Point', 'coordinates': [0, 0]}}
        for i in range(n)]}


def test_distinct_feature_ids_collapses_segments_of_one_cable():
    """★ Fails on pre-fix code, where counts.cables was len(features)."""
    p = _proxy_mod()
    fc = _fake_cable_fc()
    assert len(fc['features']) == 8
    assert p._distinct_feature_ids(fc) == 5, (
        'a cable that lands in several places arrives as several '
        'MultiLineString features under ONE id; counting features publishes a '
        'route-segment count as a cable count')


def test_proxy_counts_publish_cables_and_segments_under_distinct_names():
    """The wrong figure must stay OBSERVABLE, under a name that says what it is."""
    p = _proxy_mod()
    cables, landings = _fake_cable_fc(), _fake_landing_fc(4)
    out = p.build_counts(cables, landings)
    counts = out['counts']

    assert counts['cables'] == 5, (
        '`cables` must be the distinct-cable count — that is what the key name '
        'claims and what a consumer reads it as')
    assert counts['cable_features'] == 8, (
        'the segment count must remain published rather than deleted, so a '
        'consumer who saw the old figure can find out which unit it was')
    assert counts['cables'] < counts['cable_features'], (
        'with repeated ids present, distinct cables must be strictly fewer '
        'than features — if these are equal the distinct count is not being '
        'computed')
    assert counts['landings'] == 4
    assert counts['landing_features'] == 4


def test_proxy_counts_state_their_unit():
    """A published figure must carry its basis. Never a number without a unit."""
    p = _proxy_mod()
    out = p.build_counts(_fake_cable_fc(), _fake_landing_fc())
    basis = out.get('counts_basis') or {}

    for key in ('cables', 'cable_features', 'landings', 'landing_features'):
        assert key in basis, '%s must state its unit' % key
        assert len(basis[key]) > 25, (
            '%s needs a real unit statement, not a label' % key)
    assert 'segment' in basis['cable_features'].lower(), (
        'the feature count must be labelled a SEGMENT count so nobody '
        'republishes it as cables')
    assert 'distinct' in basis['cables'].lower()


def test_a_feature_without_an_id_is_never_silently_merged():
    """Unidentifiable features must over-count, never collapse into one cable.

    Rounding the wrong way here would understate the population, which is the
    failure mode the house rule about floors exists to prevent.
    """
    p = _proxy_mod()
    fc = {'features': [
        {'properties': {}, 'geometry': {}},
        {'properties': {}, 'geometry': {}},
        {'properties': {'id': 'echo'}, 'geometry': {}},
        {'properties': {'id': 'echo'}, 'geometry': {}},
    ]}
    # 2 unidentifiable + 1 distinct id = 3, not 1 and not 2.
    assert p._distinct_feature_ids(fc) == 3


def test_proxy_counts_survive_an_empty_or_malformed_payload():
    """The counts helper must not raise on a shape it did not expect."""
    p = _proxy_mod()
    for fc in ({}, {'features': None}, {'features': []},
               {'features': [{'properties': None}]}):
        out = p.build_counts(fc, fc)
        assert isinstance(out['counts']['cables'], int)
        assert isinstance(out['counts']['cable_features'], int)


# ══════════════════════════════════════════════════════════════════════
# 4. THE PUBLIC /api/v1/submarine-cables RESPONSE BODY
# ══════════════════════════════════════════════════════════════════════

class _FakeCursor:
    """Records every SQL statement and answers from a scripted queue."""

    def __init__(self, script):
        self.script = list(script)
        self.sql = []
        self._rows = []

    def execute(self, sql, params=None):
        self.sql.append(' '.join(str(sql).split()))
        self._rows = self.script.pop(0) if self.script else []

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def rollback(self):
        pass

    def close(self):
        pass


def _stub_main(monkeypatch):
    """Install a STUB `main` module so driving a view cannot import main.py.

    ★ House rule: tests never import main.py — it opens DB pools, starts
    keepalive threads and registers ~200 blueprints. Every view in this module
    ends with `finally: from main import return_pg_connection`, so calling one
    for real would import it. Without this stub the first version of this file
    took 53s, printed "LinkedIn init failed" and "DATABASE POOL: Retry 1/3", and
    leaked a brain-l20-durability watcher thread into the session.

    monkeypatch.setitem restores sys.modules afterwards, so a genuine `main`
    belonging to another test is not clobbered.
    """
    import types
    stub = types.ModuleType('main')
    stub.return_pg_connection = lambda conn: None
    monkeypatch.setitem(sys.modules, 'main', stub)
    return stub


def _drive_submarine_cables(monkeypatch, script, query_string=''):
    """Call the REAL view function with a fake DB and no request memoisation."""
    import flask
    m = _mod()
    _stub_main(monkeypatch)
    cur = _FakeCursor(script)
    monkeypatch.setattr(m, '_get_db', lambda: _FakeConn(cur), raising=False)
    monkeypatch.setattr(m, '_INFRA_MEMO', {}, raising=False)
    app = flask.Flask('subsea_fence_app')
    with app.test_request_context('/api/v1/submarine-cables?' + query_string):
        resp = m.get_submarine_cables()
    body = resp.get_json() if hasattr(resp, 'get_json') else resp
    return body, cur


def test_no_test_in_this_file_imported_main():
    """★ Meta-guard. A stub that stops working must be loud, not slow.

    If `main` ever ends up genuinely imported by this file, the suite silently
    gains DB pools and background threads. Assert the module object present in
    sys.modules is either absent or has no real Flask app attached.
    """
    real = sys.modules.get('main')
    if real is not None:
        assert not hasattr(real, 'app'), (
            'main.py was really imported by the test session — the `main` stub '
            'is not being installed before the views are driven')


def test_public_endpoint_queries_the_populated_tables(monkeypatch):
    """★ Fails on pre-fix code, which returned 200 + cable_count 0 as a success.

    Live before this change, keyless:
        {"success": true, "cable_count": 0, "cables": [],
         "landing_count": 0, "landings": []}
    beside a section header promising "690 cables worldwide".
    """
    cable_rows = [(1, 'echo', 'Echo', None, None, '', None, 'http://x')]
    lp_rows = [(1, 'aberdeen-united-kingdom', 'Aberdeen, United Kingdom', '',
                57.15, -2.10, '[]')]
    body, cur = _drive_submarine_cables(monkeypatch, [
        cable_rows,                       # SELECT ... FROM subsea_cables
        [(LIVE_SUBSEA_CABLES,)],          # COUNT(*) subsea_cables
        lp_rows,                          # SELECT ... FROM subsea_landing_points
        [(LIVE_SUBSEA_LANDING_POINTS,)],  # COUNT(*) subsea_landing_points
    ])

    joined = ' | '.join(cur.sql)
    assert 'FROM subsea_cables' in joined, (
        'the endpoint must read subsea_cables. SQL issued: %s' % joined)
    assert 'FROM subsea_landing_points' in joined, (
        'the endpoint must read subsea_landing_points. SQL issued: %s' % joined)
    for abandoned in ABANDONED_TABLES:
        assert abandoned not in joined, (
            'the endpoint still reads the abandoned 0-row table %r: %s'
            % (abandoned, joined))

    assert body['success'] is True
    assert body['cable_count'] == LIVE_SUBSEA_CABLES
    assert body['landing_count'] == LIVE_SUBSEA_LANDING_POINTS
    assert body['cables'], 'the cable array must no longer be empty'
    assert body['landings'], 'the landing array must no longer be empty'


def test_count_is_the_population_not_the_page_size(monkeypatch):
    """`cable_count` must not become "100 cables worldwide" under limit=100.

    The old code published len(cables). That was harmlessly 0 against an empty
    table; against a populated one it would publish the page size under a key
    a consumer reads as a population.
    """
    cable_rows = [(i, 'c%d' % i, 'C%d' % i, None, None, '', None, None)
                  for i in range(3)]
    body, _ = _drive_submarine_cables(monkeypatch, [
        cable_rows,
        [(LIVE_SUBSEA_CABLES,)],
        [],
        [(LIVE_SUBSEA_LANDING_POINTS,)],
    ], query_string='limit=3')

    assert body['cable_count'] == LIVE_SUBSEA_CABLES, (
        'cable_count must be the whole layer, unaffected by `limit`')
    assert body['cables_returned'] == 3, (
        'the page size must still be published, under its own name')
    assert body['cable_count'] != body['cables_returned']
    basis = body.get('counts_basis') or {}
    assert 'cable_count' in basis and 'cables_returned' in basis, (
        'both counts must say which one they are')


def test_unpopulated_attributes_are_null_and_declared(monkeypatch):
    """Repointing cannot conjure attributes. It must not imply it did.

    Live column census 2026-07-29: owners, length_km, rfs_year, is_planned and
    rfs_date are empty on all 691 cable rows; country, country_code and
    cable_ids are empty on all 1,908 landing rows. Upstream cable-geo.json
    publishes only id/name/url/geometry per feature.
    """
    cable_rows = [(1, 'echo', 'Echo', None, None, '', None, '')]
    lp_rows = [(1, 'lp', 'LP', '', 1.0, 2.0, '[]')]
    body, _ = _drive_submarine_cables(monkeypatch, [
        cable_rows, [(LIVE_SUBSEA_CABLES,)], lp_rows,
        [(LIVE_SUBSEA_LANDING_POINTS,)],
    ])

    cable = body['cables'][0]
    for field in ('length_km', 'rfs_year', 'owners', 'is_planned', 'status'):
        assert cable[field] is None, (
            '%s is unpopulated on every row; it must be null, not an empty '
            'string and never a fabricated default' % field)
    landing = body['landings'][0]
    assert landing['country'] is None, (
        "country is '' on every row; normalise to null so a consumer cannot "
        "read '' as a known-blank country")
    assert landing['cable_ids'] is None, (
        "cable_ids is '[]' on every row and carries no attribution")

    basis = body.get('subsea_basis') or {}
    assert basis.get('as_of'), 'the snapshot must state its vintage'
    assert basis.get('unit'), 'the snapshot must state its unit'
    unpopulated = basis.get('attributes_unpopulated') or []
    for field in ('owners', 'length_km', 'rfs_year', 'country', 'cable_ids'):
        assert field in unpopulated, (
            '%s is returned as null on every row and must be DECLARED '
            'unpopulated, or a consumer will read the null as "unknown for '
            'this cable" rather than "never collected"' % field)
    assert basis.get('drift'), (
        'the snapshot has a measured two-way drift and no delete path; that '
        'must be published, not left for a consumer to discover')


def test_country_filter_refuses_rather_than_answering_zero(monkeypatch):
    """★ A filter that cannot bind must not answer 0.

    country is '' on all 1,908 rows, so `WHERE UPPER(country) = 'JP'` matches
    nothing for EVERY country. Answering 0 asserts "Japan has no cable
    landings" — a claim this snapshot cannot make.
    """
    body, cur = _drive_submarine_cables(monkeypatch, [
        [], [(LIVE_SUBSEA_CABLES,)], [],
    ], query_string='country=JP')

    joined = ' | '.join(cur.sql)
    assert 'UPPER(country)' not in joined, (
        'the unappliable country filter must not be sent to the database at '
        'all: %s' % joined)
    assert body['landing_count'] is None, (
        'landing_count must be null for a filter that could not be applied, '
        'never 0')
    reason = (body.get('unmeasured') or {}).get('landing_count')
    assert reason and len(reason) > 40, (
        'refusing a filter silently is no better than answering 0 — it must '
        'say why')
    assert 'not applied' in reason.lower()
    applied = ((body.get('counts_basis') or {}).get('filters_applied') or {})
    assert applied.get('country') is False, (
        'the payload must state that the country filter was NOT applied')


def test_spatial_filter_still_binds_and_counts_the_filtered_population(monkeypatch):
    """The spatial filter DOES bind (latitude/longitude are populated on all rows).

    Guards the column rename: the abandoned table used lat/lng, the populated
    one uses latitude/longitude. Reading the old names would raise.
    """
    body, cur = _drive_submarine_cables(monkeypatch, [
        [], [(LIVE_SUBSEA_CABLES,)],
        [(1, 'lp', 'LP', '', 30.0, -95.0, '[]')],
        [(7,)],
    ], query_string='lat=30&lng=-95&radius=100')

    joined = ' | '.join(cur.sql)
    assert 'latitude BETWEEN' in joined, (
        'subsea_landing_points stores latitude/longitude, not lat/lng: %s' % joined)
    assert 'longitude BETWEEN' in joined
    assert body['landing_count'] == 7, (
        'a spatially filtered count is a real filtered population and must be '
        'published as a number, not suppressed')
    applied = ((body.get('counts_basis') or {}).get('filters_applied') or {})
    assert applied.get('spatial') is True


def test_cable_landing_points_empty_table_is_unmeasured_not_zero(monkeypatch):
    """/api/v1/cable-landing-points published count 0 for a never-populated table.

    NOT repointed to subsea_landing_points: it is one row per CABLE-PER-LANDING
    (cable_id + city), a different unit from one row per landing point. A
    repoint that silently changes the unit is the defect, not the fix.
    """
    import flask
    m = _mod()
    _stub_main(monkeypatch)
    cur = _FakeCursor([[], [(0,)]])  # no rows, then COUNT(*) = 0
    monkeypatch.setattr(m, '_get_db', lambda: _FakeConn(cur), raising=False)
    monkeypatch.setattr(m, '_INFRA_MEMO', {}, raising=False)
    app = flask.Flask('subsea_fence_app')
    with app.test_request_context('/api/v1/cable-landing-points'):
        body = m.get_cable_landing_points().get_json()

    assert body['count'] is None, (
        'a table that was never populated is UNMEASURED; publishing 0 asserts '
        'there are no cable landings anywhere')
    reason = (body.get('unmeasured') or {}).get('count')
    assert reason and 'cable_landing_points' in reason
    assert 'unit' in reason.lower(), (
        'the reason must explain why this is not simply repointed at '
        'subsea_landing_points — the units differ')
    assert 'FROM cable_landing_points' in ' '.join(cur.sql)


def test_cable_landing_points_real_zero_stays_a_number(monkeypatch):
    """A filter that legitimately matches nothing must still answer 0.

    The unmeasured branch must key on the TABLE being empty, not on the RESULT
    being empty — otherwise every over-narrow filter reports itself broken.
    """
    import flask
    m = _mod()
    _stub_main(monkeypatch)
    cur = _FakeCursor([[], [(4321,)]])  # no matches, but the table has rows
    monkeypatch.setattr(m, '_get_db', lambda: _FakeConn(cur), raising=False)
    monkeypatch.setattr(m, '_INFRA_MEMO', {}, raising=False)
    app = flask.Flask('subsea_fence_app')
    with app.test_request_context('/api/v1/cable-landing-points?country=ZZ'):
        body = m.get_cable_landing_points().get_json()

    assert body['count'] == 0, (
        'the table holds 4,321 rows and the filter matched none of them — that '
        'is a genuine 0 and must be published as one')
    assert 'unmeasured' not in body


# ══════════════════════════════════════════════════════════════════════
# MUST-FAIL CONTROL
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.xfail(strict=True, reason=(
    'MUST-FAIL control — deliberately asserts a falsehood. strict=True means '
    'pytest reports it as xfailed, and would report a hard FAILURE if it ever '
    'started passing. Its job is to prove this file is really being collected '
    'and executed: a collection abort or a module-scope exit yields exit 3 with '
    'ZERO tests run, which renders as an ordinary red job. If this control is '
    'not listed as xfailed in the run summary, the suite did not run.'))
def test_must_fail_control_proving_this_file_actually_executes():
    m = _mod()
    by_key = {k: t for k, t, _r in m._STATS_MEMBERS}
    # False by construction after the repoint, and false before it too (the
    # pre-fix table was `submarine_cables`), so this control cannot pass on
    # either side of the change.
    assert by_key['submarine_cables'] == 'this_table_does_not_exist'
