#!/usr/bin/env python3
"""Every bulk facility feed goes through the ONE tier ladder. NO NETWORK, NO DB.

Exact facility location (street address, precise coordinates) is paid-only.
util/facility_tier_gate.py is the single ladder that enforces it, and these
routes served facility rows past it to callers with no key and no cookie:

    /ai/learn/facilities               7 dp + power_mw, 500 rows/page, next_offset
                                       walks the whole registry
    /api/discovery/facilities          @require_plan('pro'), but the plan gate lets
                                       a dchub.cloud Referer through and the edge
                                       worker sets that Referer on every request
    /api/agent/facilities              SELECT *: exact coordinates, street address,
                                       raw_data
    /api/search/facilities             4 dp + power_mw
    /api/v1/map/public                 5,000 markers at 6 dp in one request
    /api/v1/facility-risk-delta        one row per sequential id, 7 dp
    /api/v1/intelligence/portfolio/..  500 rows per ILIKE match, 6 dp + power_mw
    /api/v1/announcements              1,000 pipeline rows, 4 dp
    /api/v1/facilities/<id>/infrastructure
                                       the facility's point, plus full-precision
                                       distances to public substations from it
    /api/v1/land-power/snapshot        500 facilities per free bbox, 7 dp

For each one: an anonymous caller gets coordinates at the anonymous rung
(<= 2 dp) and no raw_data, street address or power_mw; a free key gets its own
rung; a paid key ('developer') gets the record exactly as before.

Harness: tests never import main.py. ai_interconnection runs a DB call at
import time that reaches main.py through db_utils, so its handler is compiled
out of the source with `ast` and run against stubs; every other module is
importable on its own and its real blueprint is registered on a bare Flask app
with its database seam replaced by FakeDB below. The caller's tier comes from
the real api_tier_gating.get_request_tier(), monkeypatched per test except
where the test is about resolution itself.
"""
import ast
import contextlib
import datetime as dt
import json
import pathlib
import re

import pytest
from flask import Flask

import api_tier_gating
from util.facility_tier_gate import coord_dp_for_tier

ROOT = pathlib.Path(__file__).resolve().parents[1]

PAID = 'developer'

# Survey-grade coordinates (7 dp, the precision measured live) and every column
# a paid key pays for. Synthetic: no real site, operator or country.
LAT, LON = 12.3456789, -45.6789012
POWER = 120.0
PROVIDER = 'Example Operator'
ADDRESS = '100 Example Way'
RAW_DATA = json.dumps({'tags': {'addr:housenumber': '100',
                                'addr:street': 'Example Way'},
                       'market': 'examplemetro', 'land_acres': 40,
                       'notes': 'next to 100 Example Way'})

_LAT_KEYS = ('latitude', 'lat')
_LON_KEYS = ('longitude', 'lon', 'lng')


@pytest.fixture(autouse=True)
def _ladder_defaults(monkeypatch):
    """The ladder's own defaults: anon 2 dp, free 3 dp. A developer shell with
    MAP_ANON_COORD_DP=6 (the kill switch) must not turn every test green."""
    monkeypatch.delenv('MAP_ANON_COORD_DP', raising=False)
    monkeypatch.delenv('MAP_FREE_COORD_DP', raising=False)


@pytest.fixture
def as_tier(monkeypatch):
    """Resolve every request in this test to `tier`, through the real seam the
    handlers call (they import get_request_tier at request time)."""
    def _set(tier):
        monkeypatch.setattr(api_tier_gating, 'get_request_tier', lambda: tier)
    return _set


# ── the fake database ───────────────────────────────────────────────────────
class Row(dict):
    """A row that answers by name AND by index and unpacks as a tuple — the
    union of what db_utils.PGRowProxy, RealDictRow and a plain tuple offer, so
    one fixture serves `row['name']`, `row[0]`, `dict(row)` and unpacking."""

    def __init__(self, cols, values):
        super().__init__(zip(cols, values))
        self._values = list(values)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)

    def __iter__(self):
        return iter(self._values)


class FakeCursor:
    def __init__(self, db):
        self._db, self._rows, self.description = db, [], None

    def execute(self, sql, params=None):
        self._db.statements.append((sql, params))
        cols, rows = self._db.respond(sql, params)
        self.description = [(c, None, None, None, None, None, None) for c in cols]
        self._rows = [Row(cols, r) for r in rows]

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, db):
        self._db = db

    def cursor(self, *args, **kwargs):
        return FakeCursor(self._db)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class FakeDB:
    """Answers each statement from `routes` — (substring, cols, rows) triples,
    first match wins. A statement nothing matches FAILS the test rather than
    returning an empty result: a fake that answered everything with [] would
    let a handler pass by serving nothing."""

    def __init__(self, routes):
        self.routes, self.statements = routes, []

    def respond(self, sql, params):
        flat = ' '.join(sql.split())
        for needle, cols, rows in self.routes:
            if needle in flat:
                return cols, rows
        raise AssertionError(f'FakeDB has no answer for: {flat[:160]}')

    def connect(self, *args, **kwargs):
        return FakeConn(self)


# ── assertions ──────────────────────────────────────────────────────────────
def _dp(value):
    """Decimal places of a coordinate as it is serialised."""
    text = repr(float(value))
    return len(text.split('.')[1].rstrip('0')) if '.' in text else 0


def _coords(obj):
    """Every (key, value) coordinate pair anywhere in a JSON payload."""
    found, stack = [], [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                if k in _LAT_KEYS + _LON_KEYS and isinstance(v, (int, float)) \
                        and not isinstance(v, bool):
                    found.append((k, v))
                else:
                    stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)
    return found


def _values(obj, key):
    """Every non-null value stored under `key` anywhere in a JSON payload."""
    out, stack = [], [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                if k == key and v not in (None, '', [], {}):
                    out.append(v)
                stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)
    return out


def assert_gated(payload, tier, withheld=('power_mw',)):
    """Coordinates at `tier`'s rung (never finer than 2 dp for anon), and no
    paid column carrying a value. The payload must still carry coordinates:
    a gate that deleted them all would pass a precision check vacuously."""
    rung = coord_dp_for_tier(tier)
    assert rung is not None, f"{tier} is not a gated tier"
    if tier == 'anon':
        assert rung <= 2, "anonymous callers get at most ~1.1 km"
    coords = _coords(payload)
    assert coords, "the gated payload carries no coordinates at all"
    too_fine = [(k, v) for k, v in coords if _dp(v) > rung]
    assert not too_fine, f"{tier} still receives coordinates finer than {rung} dp: {too_fine}"
    body = json.dumps(payload, default=str)
    for secret in (ADDRESS, 'addr:housenumber'):
        assert secret not in body, f"{tier} still receives {secret!r}"
    for key in ('raw_data', 'address') + tuple(withheld):
        leaked = _values(payload, key)
        assert not leaked, f"{tier} still receives {key}={leaked[:2]}"


def assert_exact(payload):
    """The paid record: coordinates exactly as stored, never rounded."""
    coords = _coords(payload)
    assert coords, "the paid payload carries no coordinates"
    lats = {v for k, v in coords if k in _LAT_KEYS}
    lons = {v for k, v in coords if k in _LON_KEYS}
    assert lats == {LAT} and lons == {LON}, (
        f"a paid key must get the stored coordinates, got lat={lats} lon={lons}")


def test_the_fixture_is_finer_than_every_gated_rung():
    """Control: if the fixture were already coarse, every gated assertion in
    this file would pass against an ungated handler."""
    for tier in ('anon', 'free'):
        assert _dp(LAT) > coord_dp_for_tier(tier) and _dp(LON) > coord_dp_for_tier(tier)
    assert coord_dp_for_tier(PAID) is None


# ── 1. /ai/learn/facilities ─────────────────────────────────────────────────
_LEARN_COLS = ['name', 'provider', 'city', 'state', 'country', 'latitude',
               'longitude', 'power_mw', 'source', 'last_updated']
_LEARN_ROW = ('Example Campus A', PROVIDER, 'Exampleville', 'EX', 'ZZ', LAT, LON,
              POWER, 'operator-disclosure', '2026-09-01T00:00:00')


def _ai_learn_facilities(db):
    """The shipped handler, compiled out of ai_interconnection.py.

    Importing the module runs init_ai_tracking_table(), whose get_db() reaches
    main.py — so the function is lifted out with ast instead, after proving it
    is the one the /ai/learn/facilities route actually serves.
    """
    import flask
    src = (ROOT / 'ai_interconnection.py').read_text(encoding='utf-8')
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == 'ai_learn_facilities')
    routes = [d.args[0].value for d in fn.decorator_list
              if isinstance(d, ast.Call) and d.args and isinstance(d.args[0], ast.Constant)]
    assert routes == ['/ai/learn/facilities'], (
        f"ai_learn_facilities no longer serves /ai/learn/facilities: {routes}")
    fn.decorator_list = []

    @contextlib.contextmanager
    def _db_conn():
        yield db.connect()

    ns = {'request': flask.request, 'jsonify': flask.jsonify, '_db_conn': _db_conn,
          'track_ai_usage': lambda *a, **k: None}
    exec(compile(ast.Module(body=[fn], type_ignores=[]),
                 str(ROOT / 'ai_interconnection.py'), 'exec'), ns)
    return ns['ai_learn_facilities']


def _learn(tier_setter, tier):
    tier_setter(tier)
    db = FakeDB([
        ('FROM discovered_facilities ORDER BY last_updated', _LEARN_COLS, [_LEARN_ROW]),
        ('SELECT COUNT(*) FROM discovered_facilities', ['count'], [(1,)]),
    ])
    handler = _ai_learn_facilities(db)
    app = Flask(__name__)
    with app.test_request_context('/ai/learn/facilities?limit=2&offset=0'):
        resp = handler()
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    return resp.get_json()


@pytest.mark.parametrize('tier', ['anon', 'free'])
def test_ai_learn_facilities_is_gated(as_tier, tier):
    body = _learn(as_tier, tier)
    assert_gated(body, tier)
    item = body['learning_data'][0]
    # The learning shape is kept — every key, pagination included.
    assert set(item) == {'fact', 'structured', 'citation', 'updated'}
    assert set(item['structured']) >= {'name', 'operator', 'location',
                                       'coordinates', 'power_mw'}
    assert body['next_offset'] is None and body['total_records'] == 1
    assert item['structured']['power_mw'] is None
    assert item['structured']['coordinates_status'] == f"approximate_{coord_dp_for_tier(tier)}dp"
    if tier == 'anon':
        assert PROVIDER not in item['fact'] and 'Unknown' not in item['fact'], (
            "the operator is withheld from anon — the fact must neither name it "
            "nor claim it is unknown")


def test_ai_learn_facilities_paid_is_the_full_record(as_tier):
    body = _learn(as_tier, PAID)
    assert_exact(body)
    item = body['learning_data'][0]
    assert item['structured']['power_mw'] == POWER
    assert item['structured']['operator'] == PROVIDER
    assert item['fact'] == (f"Example Campus A is a data center operated by {PROVIDER} "
                            "in Exampleville, EX ZZ")
    assert item['updated'] == '2026-09-01T00:00:00'
    assert 'coordinates_status' not in item['structured']


# ── 2. /api/discovery/facilities ────────────────────────────────────────────
_DISC_COLS = ['id', 'source', 'source_id', 'name', 'provider', 'city', 'state',
              'country', 'latitude', 'longitude', 'power_mw', 'status',
              'confidence_score', 'discovered_at', 'merged_at']
_DISC_ROW = (4242, 'operator-disclosure', 'src-1', 'Example Campus A', PROVIDER,
             'Exampleville', 'EX', 'ZZ', LAT, LON, POWER, 'Operational', 0.9,
             '2026-09-01', None)


@pytest.fixture
def discovery(monkeypatch):
    """The real blueprint behind the REAL plan decorator, as main.py wires it:
    init_discovery_routes injects require_plan, so the Referer bypass under
    test is the shipped one, not a stand-in."""
    import routes.discovery_routes as mod
    db = FakeDB([
        ('FROM discovered_facilities WHERE is_duplicate = 0 ORDER BY', _DISC_COLS, [_DISC_ROW]),
        ('SELECT COUNT(*) FROM discovered_facilities', ['count'], [(1,)]),
    ])
    monkeypatch.setattr(mod, '_require_plan', api_tier_gating.require_plan)
    monkeypatch.setattr(mod, '_get_db', db.connect)
    app = Flask(__name__)
    app.register_blueprint(mod.discovery_bp)
    return app.test_client()


def test_discovery_referer_bypass_gets_gated_rows_not_the_pro_rows(discovery):
    """The load-bearing one. The plan decorator admits a keyless GET that
    carries a dchub.cloud Referer (and the edge sets that Referer on every
    proxied request). Tier is resolved for real here — no monkeypatch."""
    r = discovery.get('/api/discovery/facilities?limit=5',
                      headers={'Referer': 'https://dchub.cloud'})
    assert r.status_code == 200, (
        f"{r.status_code}: the bypass no longer admits this request, so this "
        "test is not exercising the path it exists for")
    body = r.get_json()
    assert body['success'] is True and body['count'] == 1
    assert {'data', 'count', 'total', 'limit', 'offset'} <= set(body)
    assert_gated(body, 'anon', withheld=('power_mw', 'source', 'provider',
                                         'confidence_score'))


def test_discovery_free_key_gets_the_free_rung(discovery, as_tier):
    as_tier('free')
    body = discovery.get('/api/discovery/facilities',
                         headers={'Referer': 'https://dchub.cloud'}).get_json()
    assert_gated(body, 'free', withheld=('power_mw', 'source'))
    assert body['data'][0]['provider'] == PROVIDER, "free still buys the operator"


def test_discovery_paid_is_the_full_row(discovery, as_tier):
    as_tier(PAID)
    body = discovery.get('/api/discovery/facilities',
                         headers={'Referer': 'https://dchub.cloud'}).get_json()
    assert body['data'] == [dict(zip(_DISC_COLS, _DISC_ROW))]
    assert_exact(body)


# ── 3. /api/agent/facilities ────────────────────────────────────────────────
_AGENT_COLS = ['id', 'name', 'provider', 'address', 'city', 'state', 'country',
               'latitude', 'longitude', 'lat', 'lon', 'power_mw', 'raw_data',
               'slug', 'source_url', 'status']
_AGENT_ROW = ('example-campus-a', 'Example Campus A', PROVIDER, ADDRESS,
              'Exampleville', 'EX', 'ZZ', LAT, LON, LAT, LON, POWER, RAW_DATA,
              'example-campus-a-1a2b3c4d', 'https://example.invalid/src', 'active')


@pytest.fixture
def agent(monkeypatch):
    import moltbook_integration as mod
    db = FakeDB([('SELECT * FROM facilities', _AGENT_COLS, [_AGENT_ROW])])
    monkeypatch.setattr(mod, 'get_db', db.connect)
    app = Flask(__name__)
    app.register_blueprint(mod.moltbook_bp)
    return app.test_client()


@pytest.mark.parametrize('tier', ['anon', 'free'])
def test_agent_facilities_is_gated(agent, as_tier, tier):
    as_tier(tier)
    body = agent.get('/api/agent/facilities?q=example').get_json()
    assert body['success'] is True and body['count'] == 1
    assert {'facilities', 'count', 'source'} <= set(body)
    assert_gated(body, tier, withheld=('power_mw', 'source_url'))


def test_agent_facilities_paid_is_the_full_row(agent, as_tier):
    as_tier(PAID)
    body = agent.get('/api/agent/facilities?q=example').get_json()
    assert body['facilities'] == [dict(zip(_AGENT_COLS, _AGENT_ROW))]


# ── 4. /api/search/facilities ───────────────────────────────────────────────
_SEARCH_COLS = ['id', 'name', 'provider', 'city', 'state', 'country', 'status',
                'power_mw', 'latitude', 'longitude']
_SEARCH_ROW = ('example-campus-a', 'Example Campus A', PROVIDER, 'Exampleville',
               'EX', 'ZZ', 'Operational', POWER, LAT, LON)


@pytest.fixture
def search(monkeypatch):
    import search_routes as mod
    db = FakeDB([('FROM facilities WHERE', _SEARCH_COLS, [_SEARCH_ROW])])
    monkeypatch.setattr(mod, '_get_conn', db.connect)
    app = Flask(__name__)
    mod.register_search_routes(app)
    return app.test_client()


@pytest.mark.parametrize('tier', ['anon', 'free'])
def test_search_facilities_is_gated(search, as_tier, tier):
    as_tier(tier)
    body = search.get('/api/search/facilities?q=example').get_json()
    assert body['success'] is True and body['table'] == 'facilities'
    assert_gated(body, tier)


def test_search_facilities_paid_is_the_full_row(search, as_tier):
    as_tier(PAID)
    body = search.get('/api/search/facilities?q=example').get_json()
    assert body['results'] == [dict(zip(_SEARCH_COLS, _SEARCH_ROW))]


# ── 5. /api/v1/map/public ───────────────────────────────────────────────────
_MAP_ROW = ('example-campus-a', 'Example Campus A', PROVIDER, 'Exampleville', 'EX',
            'ZZ', 'Example Region', LAT, LON, POWER, 'active', 3)
_MAP_KEYS = ['id', 'name', 'provider', 'city', 'state', 'country', 'region',
             'lat', 'lng', 'power_mw', 'status', 'tier']


@pytest.fixture
def public_map(monkeypatch):
    import public_endpoints as mod
    db = FakeDB([('FROM facilities WHERE latitude IS NOT NULL', [f'c{i}' for i in range(12)],
                  [_MAP_ROW])])
    monkeypatch.setattr(mod, 'get_read_db', db.connect)
    app = Flask(__name__)
    app.register_blueprint(mod.public_bp)
    return app.test_client()


@pytest.mark.parametrize('tier', ['anon', 'free'])
def test_public_map_is_gated_and_keeps_the_marker_shape(public_map, as_tier, tier):
    as_tier(tier)
    body = public_map.get('/api/v1/map/public').get_json()
    assert_gated(body, tier, withheld=('power_mw', 'tier'))
    marker = body['facilities'][0]
    assert set(marker) == set(_MAP_KEYS) | {'coordinates_status'}, (
        "a marker keeps every key; a withheld value is null")


def test_public_map_paid_is_the_full_marker(public_map, as_tier):
    as_tier(PAID)
    body = public_map.get('/api/v1/map/public').get_json()
    assert body['facilities'] == [dict(zip(_MAP_KEYS, _MAP_ROW))]


# ── 7. /api/v1/facility-risk-delta ──────────────────────────────────────────
_RISK_COLS = ['id', 'name', 'provider', 'city', 'state', 'market', 'latitude', 'longitude']
_RISK_ROW = (4242, 'Example Campus A', PROVIDER, 'Exampleville', 'EX',
             'examplemetro', LAT, LON)


@pytest.fixture
def risk(monkeypatch):
    import routes.facility_risk_delta as mod
    db = FakeDB([
        ('FROM discovered_facilities WHERE id::text', _RISK_COLS, [_RISK_ROW]),
        ('FROM dcpi_daily_snapshots', ['now_e'], []),
    ])
    monkeypatch.setattr(mod, '_conn', db.connect)
    app = Flask(__name__)
    app.register_blueprint(mod.facility_risk_delta_bp)
    return app.test_client()


@pytest.mark.parametrize('tier', ['anon', 'free'])
def test_risk_delta_facility_block_is_gated(risk, as_tier, tier):
    as_tier(tier)
    body = risk.get('/api/v1/facility-risk-delta?facility_id=4242').get_json()
    assert body['success'] is True
    assert_gated(body['facility'], tier)
    assert set(body['facility']) >= {'id', 'name', 'provider', 'lat', 'lon'}
    if tier == 'anon':
        assert body['facility']['provider'] is None


def test_risk_delta_paid_is_the_full_block(risk, as_tier):
    as_tier(PAID)
    body = risk.get('/api/v1/facility-risk-delta?facility_id=4242').get_json()
    assert body['facility'] == {
        'id': 4242, 'name': 'Example Campus A', 'provider': PROVIDER,
        'city': 'Exampleville', 'state': 'EX', 'market': 'examplemetro',
        'lat': LAT, 'lon': LON}


# ── 8. /api/v1/intelligence/portfolio/<provider> ────────────────────────────
_PORT_COLS = ['id', 'name', 'city', 'state', 'country', 'market', 'power_mw',
              'sqft', 'status', 'facility_type', 'latitude', 'longitude',
              'confidence_score']
_PORT_ROW = (4242, 'Example Campus A', 'Exampleville', 'EX', 'ZZ', 'examplemetro',
             POWER, 50000, 'Operational', 'hyperscale', LAT, LON, 0.9)
_STATS_COLS = ['total_facilities', 'operational', 'pipeline', 'countries',
               'us_states', 'markets', 'cities', 'total_power_mw',
               'avg_facility_mw', 'max_facility_mw', 'avg_confidence',
               'facilities_with_power', 'facilities_with_sqft', 'total_sqft']


@pytest.fixture
def portfolio(monkeypatch):
    import routes.intelligence_routes as mod
    db = FakeDB([
        ('AS total_facilities', _STATS_COLS, [(1, 1, 0, 1, 1, 1, 1, POWER, POWER,
                                               POWER, 0.9, 1, 1, 50000)]),
        ('SELECT country, COUNT(*)', ['country', 'count', 'power_mw'], [('ZZ', 1, POWER)]),
        ('SELECT market, COUNT(*)', ['market', 'count', 'power_mw'], [('examplemetro', 1, POWER)]),
        ('SELECT status, COUNT(*)', ['status', 'count'], [('Operational', 1)]),
        ('SELECT facility_type, COUNT(*)', ['facility_type', 'count'], [('hyperscale', 1)]),
        ('SELECT id, name, city, state, country, market, power_mw', _PORT_COLS, [_PORT_ROW]),
    ])
    monkeypatch.setattr(mod, '_get_pg', db.connect)
    monkeypatch.setattr(mod, '_return_pg', lambda conn: None)
    app = Flask(__name__)
    app.register_blueprint(mod.intelligence_bp)
    return app.test_client()


@pytest.mark.parametrize('tier', ['anon', 'free'])
def test_portfolio_facility_list_is_gated(portfolio, as_tier, tier):
    as_tier(tier)
    body = portfolio.get('/api/v1/intelligence/portfolio/Example').get_json()
    assert body['success'] is True
    assert_gated(body['data']['facilities'], tier, withheld=('power_mw', 'sqft'))
    assert body['data']['summary']['total_facilities'] == 1, "aggregates stay public"


def test_portfolio_paid_is_the_full_list(portfolio, as_tier):
    as_tier(PAID)
    body = portfolio.get('/api/v1/intelligence/portfolio/Example').get_json()
    assert body['data']['facilities'] == [dict(zip(_PORT_COLS, _PORT_ROW))]


# ── 9. /api/v1/announcements ────────────────────────────────────────────────
_ANN_COLS = ['id', 'name', 'provider', 'city', 'state', 'country', 'region',
             'latitude', 'longitude', 'power_mw', 'status', 'facility_type',
             'discovered_at', 'source', 'raw_data']
_ANN_ROW = (4242, 'Example Campus A', PROVIDER, 'Exampleville', 'EX', 'ZZ',
            'examplemetro', LAT, LON, POWER, 'Planned', 'hyperscale',
            '2026-09-01', 'press', RAW_DATA)


@pytest.fixture
def announcements(monkeypatch):
    import routes.deals_routes as mod
    db = FakeDB([('FROM discovered_facilities WHERE LOWER(status) IN', _ANN_COLS, [_ANN_ROW])])
    monkeypatch.setattr(mod, '_get_db', db.connect)
    app = Flask(__name__)
    app.register_blueprint(mod.deals_bp)
    return app.test_client()


@pytest.mark.parametrize('tier', ['anon', 'free'])
def test_announcements_are_gated(announcements, as_tier, tier):
    as_tier(tier)
    body = announcements.get('/api/v1/announcements').get_json()
    assert body['success'] is True and body['count'] == 1
    assert_gated(body, tier, withheld=('power_mw', 'notes', 'land_acres'))


def test_announcements_paid_is_the_full_row(announcements, as_tier):
    as_tier(PAID)
    row = announcements.get('/api/v1/announcements').get_json()['data'][0]
    assert (row['latitude'], row['longitude']) == (LAT, LON)
    assert row['power_mw'] == POWER and row['provider'] == PROVIDER
    assert row['notes'] == 'next to 100 Example Way' and row['land_acres'] == 40
    assert 'raw_data' not in row, "raw_data was already unpacked and dropped for everyone"


# ── 10. /api/v1/facilities/<id>/infrastructure ──────────────────────────────
@pytest.fixture
def infrastructure(monkeypatch):
    import dchub_iteration_2_routes as mod
    db = FakeDB([
        ('FROM facilities WHERE id = %s OR slug = %s',
         ['latitude', 'longitude', 'name', 'provider', 'power_mw'],
         [(LAT, LON, 'Example Campus A', PROVIDER, POWER)]),
        ('FROM substations', ['id', 'name', 'voltage_kv', 'operator', 'lat', 'lon',
                              'distance_km'], []),
        ('FROM gas_pipelines', ['id'], []),
        ('FROM fiber_routes', ['id'], []),
        ('to_regclass', ['exists'], [(False,)]),
    ])
    monkeypatch.setattr(mod, '_get_pg_conn', db.connect)
    app = Flask(__name__)
    mod.register_iteration_2_routes(app)
    return app.test_client(), db


def _origin(db):
    """The point the substation search measured distances FROM."""
    params = [p for sql, p in db.statements if 'FROM substations' in sql]
    assert params, "the substation query never ran"
    lat, _lat_again, lon = params[0][:3]
    return lat, lon


@pytest.mark.parametrize('tier', ['anon', 'free'])
def test_infrastructure_is_gated_and_measures_from_the_gated_point(infrastructure, as_tier, tier):
    """Rounding only the reply's lat/lon is not enough: every nearby substation
    carries an exact public position and a distance_km to the origin, so three
    of them trilaterate it. The origin itself must be the gated point."""
    client, db = infrastructure
    as_tier(tier)
    body = client.get('/api/v1/facilities/example-campus-a/infrastructure').get_json()
    assert_gated({'lat': body['lat'], 'lon': body['lon'], 'facility': body['facility']}, tier)
    lat, lon = _origin(db)
    assert (lat, lon) == (body['lat'], body['lon']), (
        f"distances were measured from {lat},{lon}, not from the gated point — "
        "they would trilaterate the exact location")


def test_infrastructure_paid_measures_from_the_exact_point(infrastructure, as_tier):
    client, db = infrastructure
    as_tier(PAID)
    body = client.get('/api/v1/facilities/example-campus-a/infrastructure').get_json()
    assert (body['lat'], body['lon']) == (LAT, LON) == _origin(db)
    assert body['facility'] == {'name': 'Example Campus A', 'provider': PROVIDER,
                                'power_mw': POWER}


# ── 11. /api/v1/land-power/snapshot ─────────────────────────────────────────
_SNAP_COLS = ['id', 'name', 'provider', 'capacity_mw', 'status', 'lat', 'lon', 'slug']
_SNAP_ROW = ('example-campus-a', 'Example Campus A', PROVIDER, POWER, 'Operational',
             LAT, LON, 'example-campus-a-1a2b3c4d')


# ★ 2026-09-22 (owner): the snapshot is Land & Power, which is Pro only
# (util/plan_tease.py lp_gated_view; tests/test_land_power_pro_only.py holds its
# matrix). A keyless caller gets the wall and no layer. Below Pro the preview
# keeps the facilities layer, three rows, each still gated by the facility
# record gate for the caller's tier and then held at two decimals. So the
# callers here carry a key that resolves to the tier under test.
_SNAP_KEYS = {'dch_live_' + 'f' * 32: 'free', 'dch_live_' + 'k' * 32: 'pro'}


@pytest.fixture
def snapshot(monkeypatch):
    import dchub_iteration_2_routes as mod
    db = FakeDB([('FROM facilities WHERE latitude BETWEEN', _SNAP_COLS, [_SNAP_ROW])])
    monkeypatch.setattr(mod, '_get_pg_conn', db.connect)
    monkeypatch.setattr(api_tier_gating, 'validate_api_key',
                        lambda k: {'plan': _SNAP_KEYS[k], 'user_id': k} if k in _SNAP_KEYS else None)
    app = Flask(__name__)
    mod.register_iteration_2_routes(app)
    return app.test_client()


_BBOX = '/api/v1/land-power/snapshot?bbox=-46,12,-45,13&layers=facilities'


def _snap(client, tier):
    key = next(k for k, t in _SNAP_KEYS.items() if t == tier)
    return client.get(_BBOX, headers={'X-API-Key': key})


def test_snapshot_keyless_gets_no_layer(snapshot, as_tier):
    as_tier('anon')
    r = snapshot.get(_BBOX)
    assert r.status_code == 403 and r.get_json()['_wall'] is True
    assert 'layers' not in r.get_json()


def test_snapshot_facilities_layer_is_gated(snapshot, as_tier):
    as_tier('free')
    body = _snap(snapshot, 'free').get_json()
    assert_gated(body['layers'], 'free', withheld=('capacity_mw',))
    row = body['layers']['facilities'][0]
    assert set(row) == set(_SNAP_COLS) | {'coordinates_status'}, (
        "the layer keeps every key; a withheld value is null")


def test_snapshot_pro_is_the_full_row(snapshot, as_tier):
    as_tier('pro')
    body = _snap(snapshot, 'pro').get_json()
    assert body['layers']['facilities'] == [dict(zip(_SNAP_COLS, _SNAP_ROW))]


# ── 12. the hand-built SEO landings (routes/seo_pages.py) ───────────────────
# Same bytes for every visitor and shared-cached at the edge by URL: these are
# anonymous surfaces whoever is looking, so there is no paid variant to test.
@pytest.fixture
def seo(monkeypatch):
    import routes.seo_pages as mod
    monkeypatch.setattr(mod, '_valid_market_slugs', lambda: None)
    return mod


def _page_coords(html):
    """Every coordinate a page prints: JSON-LD geo, the fact table, map links."""
    found = [float(v) for pair in re.findall(
        r'"latitude":\s*"?(-?[\d.]+)"?,\s*"longitude":\s*"?(-?[\d.]+)"?', html) for v in pair]
    for cell in re.findall(r'<th>Coordinates</th><td>([^<]*)</td>', html):
        found += [float(v) for v in re.findall(r'-?\d+\.\d+', cell)]
    for a, b in re.findall(r'openstreetmap\.org/[^"]*?(-?\d+\.\d+)[/&=a-z]+(-?\d+\.\d+)', html):
        found += [float(a), float(b)]
    return found


def _assert_anonymous_page(html, raw_lat, raw_lon):
    coords = _page_coords(html)
    assert coords, "the page prints no coordinates, so the precision check is vacuous"
    rung = coord_dp_for_tier('anon')
    too_fine = [v for v in coords if _dp(v) > rung]
    assert not too_fine, f"an anonymous page prints coordinates finer than {rung} dp: {too_fine}"
    for raw in (raw_lat, raw_lon):
        if _dp(raw) > rung:          # an already-coarse value may print as itself
            assert str(raw) not in html, f"the stored coordinate {raw} is printed"
    assert 'mlat=' not in html and 'mlon=' not in html, (
        "a map pin asserts an exact position; link an area view instead")


def _with_survey_grade(entries):
    """The real hand-typed entries plus one synthetic copy carrying the 7 dp
    fixture point and a street address with a postcode — so each renderer is
    exercised on values finer than the anon rung however the real data is
    typed (already-coarse data would otherwise make the check vacuous)."""
    items = list(entries.items())
    key, meta = items[0]
    synthetic = dict(meta, lat=LAT, lon=LON,
                     address=f"{ADDRESS}, Exampleville, EX 99999")
    return items + [(key, synthetic)]


def test_aws_landings_print_anonymous_coordinates(seo):
    for code, meta in _with_survey_grade(seo.AWS_REGION_MAP):
        _assert_anonymous_page(seo._aws_landing_html(meta, code), meta['lat'], meta['lon'])


def test_address_landing_prints_no_street_address(seo):
    """The street address is paid-only. The URL and <title> are deliberately
    left as they are; everything else in the page must not print the stored
    street-address field — not the JSON-LD, not the meta description, not the
    breadcrumb, lede, headings or fact table."""
    for slug, meta in _with_survey_grade(seo.ADDRESS_MAP):
        html = seo._address_landing_html(meta, slug)
        _assert_anonymous_page(html, meta['lat'], meta['lon'])
        assert 'streetAddress' not in html and 'Street address' not in html
        assert meta['address'] not in html, "the stored street-address field is printed"
        postcode = meta['address'].rsplit(' ', 1)[-1]
        if postcode.isdigit():
            assert postcode not in html, "the postcode travels with the street address"
        title = re.search(r'<title>(.*?)</title>', html).group(1)
        assert title == f"{meta['query']} | DC Hub", "the <title> was to be left alone"


def test_facility_render_prints_anonymous_coordinates_and_no_pin(seo):
    row = {'id': 'example-1', 'name': 'Example Campus A', 'provider': PROVIDER,
           'city': 'Exampleville', 'state': 'EX', 'country': 'ZZ',
           'latitude': LAT, 'longitude': LON, 'power_mw': POWER,
           'status': 'active', 'sqft': 0, 'tier': 0}
    html = seo._render_facility(row, nearby=[])
    _assert_anonymous_page(html, LAT, LON)
    assert 'openstreetmap.org/#map=' in html, "the area link went missing"


# ── anti-recurrence: the NEXT bulk route ────────────────────────────────────
# A route handler that SELECTs coordinates out of a facility table must call a
# shared gate. Each exemption is a measurement or a registration fact, not an
# opinion: re-check it before adding to the list.
_FACILITY_TABLE = re.compile(
    r"\bFROM\s+(discovered_facilities|facilities|carrier_facility_presence)\b", re.I)
_COORD = re.compile(r"\b(latitude|longitude|facility_lat|facility_lng|lat|lng|lon)\b"
                    r"|SELECT\s+\*", re.I)
_SHARED_GATES = {'gate_record', 'gate_records', 'coarsen_coords_deep',
                 'apply_record_gate'}
_NOT_SCANNED = {
    'main.py',                          # its own guard: tests/test_facility_tier_gate.py
    'routes/facility_profile_page.py',  # its own guards, owned with the page
    'api_fixes.py', 'api_server.py',    # never imported: gunicorn serves main:app
    'deals_routes.py',                  # the pre-move copy; main.py imports routes.deals_routes
}
_EXEMPT = {
    # Licensed-partner deliverable whose exact coordinates and capacity are part
    # of the licence. It has never required a credential, so gating it could
    # degrade the licensee's scheduled pull. Held open until the licensee's
    # credential path is confirmed (owner decision 2026-09-21).
    'reveal_endpoints.py:reveal_validation_feed',
    # Serves distances and counts only; no facility coordinate leaves it.
    'carrier_facility_ingestion.py:fiber_nearby_api',
    'routes/cross_layer_sites.py:cross_layer_sites',
    'routes/mcp_tier1_tools.py:find_alternatives',
    'routes/mcp_tier1_tools.py:score_facility',
    'routes/data_quality_routes.py:facility_quality',   # populated-or-not booleans
    # Credential-gated before any row is read.
    'routes/facility_dedup.py:dedup_export',            # admin key
    'routes/mcp_funnel_diag.py:backfill_empty_city',    # internal key
    'routes/mcp_funnel_diag.py:backfill_market_by_bbox',  # internal key
    'routes/mcp_tier2_reports.py:export_facility_csv',  # key + tier, 401/402
    'routes/r2_exports.py:license_facilities',          # enterprise plan
    # Not registered in production: register_data_layers() runs only under
    # `if __name__ == '__main__'`; live, the path is answered by the
    # /api/v1/facilities/<facility_id> handler ("Facility not found").
    'data_layers_api.py:get_all_facilities',
}


def _called_names(fn):
    return {c.func.id if isinstance(c.func, ast.Name) else getattr(c.func, 'attr', '')
            for c in ast.walk(fn) if isinstance(c, ast.Call)}


def _route_handlers():
    """(path, handler, every name the handler reaches through this module's own
    functions). Handlers are decorated routes AND add_url_rule view functions;
    reach follows module-local calls a few levels, so a route that gates through
    its own renderer (seo_pages: facility_page -> _render_facility ->
    _anon_coords -> coarsen_coords_deep) counts as gated."""
    for py in sorted(ROOT.rglob('*.py')):
        rel_parts = py.relative_to(ROOT).parts
        rel = '/'.join(rel_parts)
        if (rel_parts[0] in ('tests', 'scripts', 'tools') or rel in _NOT_SCANNED
                or any(p.startswith('.') or p in ('venv', 'node_modules', 'site-packages')
                       for p in rel_parts)):
            continue
        try:
            src = py.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            continue
        if not _FACILITY_TABLE.search(src):
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        funcs = [n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        by_name = {}
        for f in funcs:
            by_name.setdefault(f.name, f)
        views = set()
        for c in ast.walk(tree):
            if (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                    and c.func.attr == 'add_url_rule'):
                cand = [k.value for k in c.keywords if k.arg == 'view_func']
                cand += c.args[2:3]
                views |= {v.id for v in cand if isinstance(v, ast.Name)}
        for fn in funcs:
            decorated = any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                            and d.func.attr in ('route', 'get', 'post')
                            for d in fn.decorator_list)
            if not decorated and fn.name not in views:
                continue
            reach, frontier = set(), [fn]
            for _depth in range(4):
                nxt = []
                for f in frontier:
                    for name in _called_names(f) - reach:
                        reach.add(name)
                        if name in by_name and by_name[name] is not fn:
                            nxt.append(by_name[name])
                frontier = nxt
            yield rel, fn, reach


def test_every_route_selecting_facility_coordinates_calls_a_shared_gate():
    """'Gating the map did not gate the corpus' is how this class recurs: each
    fix closed one route and the next route re-opened it. This fails on the
    next one. A CALL is required — an import alone survives deleting the call."""
    offenders, seen = [], set()
    for rel, fn, reach in _route_handlers():
        sql = [n.value for n in ast.walk(fn)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)
               and 'SELECT' in n.value.upper() and _FACILITY_TABLE.search(n.value)]
        if not any(_COORD.search(s) for s in sql):
            continue
        key = f"{rel}:{fn.name}"
        seen.add(key)
        if not reach & _SHARED_GATES and key not in _EXEMPT:
            offenders.append(key)
    assert len(seen) >= 12, (
        f"the scan found only {len(seen)} handlers — it has gone blind, and an "
        "empty offender list would prove nothing")
    stale = sorted(_EXEMPT - seen)
    assert not stale, f"exemptions that no longer match a handler (remove them): {stale}"
    assert offenders == [], (
        "these route handlers SELECT facility coordinates and never call "
        f"util.facility_tier_gate: {offenders}")
