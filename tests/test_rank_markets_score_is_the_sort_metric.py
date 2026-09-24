"""Guards for #3765: rank_markets `score` is the metric the rows are sorted by.

Measured live 2026-09-04 (anonymous rank_markets, sweeping `limit`):

    limit=3   ashburn(r1)=100  dallas(r2)=66.7  chicago(r3)=33.3
    limit=50  ashburn(r1)=100  dallas(r2)=98    chicago(r3)=96

i.e. score = 100 x (N - rank + 1) / N, a position ladder that moved with the
caller's `limit`, while the same response's `methodology` said
"Composite: 0.4xtotal_mw + 50xoperators + 20xfacilities".

These tests drive the real Flask view against a fake DB connection (no
network). The fake returns fixed aggregate rows; it does not evaluate SQL, so
the tests assert on the VALUES the view publishes for those rows, and
separately on the ORDER BY text the view sent, never on a copy of the formula
living only in the test.
"""
from contextlib import contextmanager
from decimal import Decimal

import pytest
from flask import Flask

import routes.mcp_tier1_tools as m


# The three markets from the issue. Inputs are fixtures (total_mw a Decimal, as
# numeric(10,1) arrives from psycopg2), chosen so the composites equal the ones
# the issue computed from live published fields:
# composite = 0.4*mw + 50*ops + 20*fac  ->  8887.2 / 5097.2 / 4489.2
_ROWS = [
    {"slug": "ashburn-va", "city": "Ashburn", "state": "VA", "country": "US",
     "facility_count": 101, "total_mw": Decimal("15543.0"), "operator_count": 13,
     "avg_mw": 0, "max_mw": 0},
    {"slug": "dallas-tx", "city": "Dallas", "state": "TX", "country": "US",
     "facility_count": 97, "total_mw": Decimal("7518.0"), "operator_count": 3,
     "avg_mw": 0, "max_mw": 0},
    {"slug": "chicago-il", "city": "Chicago", "state": "IL", "country": "US",
     "facility_count": 76, "total_mw": Decimal("6923.0"), "operator_count": 4,
     "avg_mw": 0, "max_mw": 0},
]


class _Cur:
    def __init__(self, sink):
        self.sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        self.sink["sql"] = sql
        self.limit = params[-1]            # LIMIT %s is the last param

    def fetchall(self):
        return [dict(r) for r in _ROWS[: self.limit]]


class _Conn:
    def __init__(self, sink):
        self.sink = sink

    def cursor(self, cursor_factory=None):
        return _Cur(self.sink)


@pytest.fixture
def client(monkeypatch):
    sink = {}

    @contextmanager
    def fake_conn():
        yield _Conn(sink)

    monkeypatch.setattr(m, "_conn", fake_conn)
    if m.psycopg2 is None:                 # view names RealDictCursor
        import types
        fake = types.SimpleNamespace(extras=types.SimpleNamespace(RealDictCursor=None))
        monkeypatch.setattr(m, "psycopg2", fake)
    app = Flask(__name__)
    app.register_blueprint(m.mcp_tier1_bp)
    c = app.test_client()
    c.sink = sink
    return c


def _call(client, **q):
    r = client.get("/api/v1/mcp/tools/rank_markets", query_string=q)
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()


def _scores(body):
    return {row["market"]: row["score"] for row in body["results"]}


def test_best_overall_score_is_the_composite_methodology_names(client):
    body = _call(client, criteria="best_overall", limit=3)
    assert body["methodology"].startswith("Composite: 0.4")
    assert _scores(body) == {
        "ashburn-va": 8887.2, "dallas-tx": 5097.2, "chicago-il": 4489.2}


def test_score_does_not_move_with_limit(client):
    """The defect: Dallas was 66.7 at limit=3 and 98 at limit=50 live; the
    old formula gives 50 vs 66.7 across limit=2 vs 3 on these fixtures."""
    small = _scores(_call(client, criteria="best_overall", limit=2))
    large = _scores(_call(client, criteria="best_overall", limit=3))
    assert small["dallas-tx"] == large["dallas-tx"] == 5097.2
    assert small["ashburn-va"] == large["ashburn-va"]


def test_score_is_not_the_old_rank_ladder(client):
    body = _call(client, criteria="best_overall", limit=3)
    n = len(body["results"])
    ladder = [round(100 * (n - row["rank"] + 1) / n, 1) for row in body["results"]]
    assert [row["score"] for row in body["results"]] != ladder
    assert body["results"][0]["score"] != 100


@pytest.mark.parametrize("criteria,field", [
    ("most_capacity", "total_mw"),
    ("cheapest_power", "total_mw"),
    ("most_operators", "operator_count"),
    ("fastest_growing", "facility_count"),
])
def test_single_metric_criteria_score_is_that_metric(client, criteria, field):
    body = _call(client, criteria=criteria, limit=3)
    for row in body["results"]:
        assert row["score"] == float(row[field]), (criteria, row)


def test_published_scores_are_in_the_order_the_rows_are_ranked(client):
    body = _call(client, criteria="best_overall", limit=3)
    s = [row["score"] for row in body["results"]]
    assert s == sorted(s, reverse=True)


def test_sql_sorts_by_the_same_weights_the_score_uses(client):
    _call(client, criteria="best_overall", limit=3)
    sql = " ".join(client.sink["sql"].split())
    w_mw, w_ops, w_fac = m._BEST_OVERALL_WEIGHTS
    assert (f"ORDER BY (COALESCE(SUM(power_mw), 0) * {w_mw} "
            f"+ COUNT(DISTINCT provider) * {w_ops} + COUNT(*) * {w_fac}) DESC") in sql
    assert m._BEST_OVERALL_WEIGHTS == (0.4, 50, 20)


def test_response_says_what_score_is(client):
    body = _call(client, criteria="best_overall", limit=3)
    assert "sorted by" in body["score_basis"]
    assert "limit" in body["score_basis"]
