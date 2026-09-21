"""A NULL deals.year / deals.type must not 500 the uncached deals response.

Measured 2026-09-21 from outside, anonymous, on a fresh cache key
(`/api/v1/deals?category=<unique>` forces a DEALS_CACHE miss on the full set):

    500 {"error":"'<' not supported between instances of 'NoneType' and 'int'"}

get_deals builds stats_by_year / stats_by_type keyed by the raw column value.
One NULL-year row put a None key beside the int years, and jsonify sorts keys
(Flask's DefaultJSONProvider.sort_keys), so every cache MISS raised TypeError.
The global @app.errorhandler(Exception) turned it into an unlogged 500. The
cache-HIT path omits both stats dicts, which is why a retry to the same worker
inside the 5-minute TTL came back 200 and the failure looked intermittent:
/api/v1/deals 49%, /api/deals 37%, keyed /api/v1/transactions 15% 5xx over 7d.

These tests run the real handler through a real Flask app and Flask's own JSON
provider, with a fake DB cursor. data_source == 'live' proves the fake rows
were served (a seed fallback would pass without exercising the NULL row).
"""
import contextlib
import itertools

import flask
import pytest

import routes.deals_routes as dr
from utils.cache import BoundedCache

# Shape of the live SELECT: id, date, year, buyer, seller, value, mw, type,
# region, market. Row 2 carries the NULL year AND the NULL type.
_ROWS = [
    (1, '2024-05-01', 2024, 'Buyer A', 'Seller B', 100.0, 50.0, 'ma', 'North America', 'Northern Virginia'),
    (2, '2023-02-01', None, 'Buyer C', 'Seller D', 0, None, None, 'EMEA', 'London'),
]

_unique = itertools.count()


class _Cursor:
    def execute(self, *_a, **_k):
        return None

    def fetchall(self):
        return list(_ROWS)


class _Conn:
    def cursor(self):
        return _Cursor()


@contextlib.contextmanager
def _fake_pg_connection():
    yield _Conn()


def _passthrough(f):
    return f


@pytest.fixture
def client(monkeypatch):
    # Non-empty so get_deals takes its PG branch (served by the fake below).
    # Loopback, so any other module that reads it fails fast and resolves
    # nothing off-box: libpq's DNS lookup is C-level, below the no-network hook.
    monkeypatch.setenv('DATABASE_URL', 'postgresql://test@127.0.0.1:1/test')
    monkeypatch.setattr(dr, 'DEALS_CACHE', BoundedCache(max_size=50, ttl=300))
    dr.init_deals_routes(
        require_plan=lambda _plan: _passthrough,
        protect_data=_passthrough,
        get_db=lambda: None,
        pg_connection=_fake_pg_connection,
        get_ai_wars_key_info=lambda: None,
    )
    app = flask.Flask('deals-stats-null-keys')
    app.register_blueprint(dr.deals_bp)
    return app.test_client()


@pytest.fixture(params=[True, False], ids=['paid', 'free'])
def paid(request, monkeypatch):
    import routes.tier_gate as tg
    monkeypatch.setattr(tg, 'caller_is_privileged', lambda *_a, **_k: request.param)
    return request.param


def _get(client, path, **headers):
    # A never-seen category value is a guaranteed DEALS_CACHE miss that filters
    # nothing out (only 'traditional' / 'hyperscaler' / 'ai' filter).
    return client.get(f'{path}?category=t{next(_unique)}', headers=headers)


@pytest.mark.parametrize('path', ['/api/deals', '/api/v1/deals'])
def test_uncached_deals_with_null_year_and_type_is_200(client, paid, path):
    resp = _get(client, path)
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    body = resp.get_json()
    assert body['data_source'] == 'live'
    assert body['tier'] == ('paid' if paid else 'free')
    assert body['stats_by_year']['2024']['count'] == 1
    assert body['stats_by_year']['unknown']['count'] == 1
    assert body['stats_by_type']['ma']['count'] == 1
    assert body['stats_by_type']['unknown']['count'] == 1


def test_keyed_transactions_reach_the_same_stats_and_are_200(client, paid):
    resp = _get(client, '/api/v1/transactions', **{'X-API-Key': 'k'})
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    body = resp.get_json()
    assert body['data_source'] == 'live'
    assert body['stats_by_year']['unknown']['count'] == 1


def test_stats_key_is_always_a_sortable_str():
    keys = [dr._stats_key(v) for v in (2024, None, 2019, 'ma', '')]
    assert keys == ['2024', 'unknown', '2019', 'ma', '']
    assert sorted(keys)  # a None among ints/strs would raise here
