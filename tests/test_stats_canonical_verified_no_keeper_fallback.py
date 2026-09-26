"""/api/v1/stats/canonical must never publish the KEEPER count as `facilities_verified`.

The public `facilities_verified` on this endpoint (and on /api/v1/stats) is
COUNT(*) WHERE duplicate_of_id IS NULL — live ~22,414. Until 2026-09-25 a
failure of that query fell back to

    stats.setdefault("facilities_verified", _cs.get("facilities_verified"))

where canonical_stats' `facilities_verified` was the deprecated alias of
facilities_with_keeper_distinct: COUNT(DISTINCT canonical_slug) WHERE
COALESCE(is_duplicate,0)=0 — a different population (~23,172), or its 400
cold-start seed. The response then carried a number from another query under
this endpoint's documented name. A failed query now publishes None (key kept,
so the response shape stays stable for the API contract guard).

Runs the real blueprint on a Flask test app with a fake DB connection. Never
imports main.py.
"""
import pytest

flask = pytest.importorskip("flask")

KEEPER = 23_172          # canonical_stats' keeper-distinct count
TRACKED = 31_000         # canonical_stats' raw `facilities`
DB_COUNT = 22_414        # what every COUNT on the fake connection answers


class _Cursor:
    def __init__(self, fail_verified):
        self._fail = fail_verified

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if self._fail and "duplicate_of_id IS NULL" in sql:
            raise RuntimeError("statement timeout")

    def fetchone(self):
        return (DB_COUNT,)

    def fetchall(self):
        return []


class _Conn:
    def __init__(self, fail_verified):
        self._fail = fail_verified

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _Cursor(self._fail)

    def rollback(self):
        pass

    def close(self):
        pass


def _get(monkeypatch, fail_verified):
    import canonical_stats
    import routes.facilities_by_dims as fbd

    # A canonical_stats snapshot that still carries the old name as well as the
    # new one: the endpoint must not reach for EITHER to fill its own
    # duplicate_of_id field, whatever a producer happens to emit.
    snap = {"facilities": TRACKED,
            "facilities_with_keeper_distinct": KEEPER,
            "facilities_verified": KEEPER,
            "deals": 1_900, "markets": 330}
    monkeypatch.setattr(canonical_stats, "get_canonical_stats",
                        lambda *a, **k: dict(snap))
    monkeypatch.setattr(fbd, "_conn", lambda: _Conn(fail_verified))

    app = flask.Flask(__name__)
    app.register_blueprint(fbd.facilities_by_dims_bp)
    resp = app.test_client().get("/api/v1/stats/canonical")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body.get("ok") is True, body
    return body["stats"]


def test_control_a_healthy_query_publishes_its_own_count(monkeypatch):
    stats = _get(monkeypatch, fail_verified=False)
    assert stats["facilities_verified"] == DB_COUNT
    assert stats["facilities_tracked"] == DB_COUNT


def test_failed_verified_query_publishes_none_not_the_keeper_count(monkeypatch):
    stats = _get(monkeypatch, fail_verified=True)
    # Anti-vacuity: the except branch really ran — tracked came from the
    # canonical_stats fallback, not from the (skipped) COUNT.
    assert stats["facilities_tracked"] == TRACKED, stats
    assert "facilities_verified" in stats, (
        "the key vanished on failure — a conditional key makes the response "
        "shape dynamic for the API contract guard; publish None instead")
    assert stats["facilities_verified"] is None, (
        f"facilities_verified = {stats['facilities_verified']!r} after its own "
        f"duplicate_of_id query failed. {KEEPER} is canonical_stats' KEEPER "
        f"count (is_duplicate-based, DISTINCT slug) — a different population "
        f"published under this endpoint's duplicate_of_id name.")
