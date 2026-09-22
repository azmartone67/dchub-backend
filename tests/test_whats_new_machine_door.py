"""/api/v1/whats-new is the machine door for the /whats-new page: it must carry
the headline counts, an as_of, and the recent items (2026-09-22).

WHY. Agents that do not speak MCP read /whats-new once. The page now serves
facilities / tools / deals / markets in its HTML; the JSON it links as the
machine-readable changelog carried items and platform cards but NO headline
counts and no `as_of` key, so an agent following the link lost the figures the
page showed. These tests render the REAL route (stubbed DB, no network) and
read the JSON an agent gets.

They also pin how the counts are read. headline_counts() must never call
resolve_canon(): that probes live per call (~10s mean) and this route has the
edge's 5s GET budget. And a cold or degraded canon must be labelled
provisional, the same verdict /api/v1/canon/phrases gives its own body.

House rules: pytest functions only, no module-scope work beyond imports, never
import main.py.
"""
import datetime

import pytest

STUB_COUNTS = {"facilities": "24,500+", "tools": 91, "deals": "1,600+", "markets": "300+"}


class _Cur:
    """Answers the route's scalar COUNT(*) reads in order; nothing else."""

    def __init__(self):
        self._vals = iter([(66,), (10,), (1974,), (24508,)])   # deals 7d, 1d, total; distinct

    def execute(self, *a, **k):
        pass

    def fetchone(self):
        return next(self._vals, (0,))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def cursor(self):
        return _Cur()

    def rollback(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_LAYER = {"layer": "substations", "count": 127365, "category": "daily", "delta_window": 39,
          "window_days": 7, "delta_1d": 1, "as_of": "2026-09-22", "last_ingest_at": None,
          "ingest_age_days": 0, "freshness_measurable": True, "expected_cadence": "daily",
          "status": "growing", "status_reason": "+39 new rows in the last 7d", "known_issue": None,
          "resolved": None, "count_captured_at": "2026-09-22T00:53:27+00:00"}


@pytest.fixture()
def client(monkeypatch):
    import routes.infra_growth as ig
    import routes.platform_updates as pu
    import routes.canon_phrases as cp

    monkeypatch.setattr(ig, "_dsn", lambda: "postgres://stub")
    monkeypatch.setattr(ig.psycopg2, "connect", lambda *a, **k: _Conn())
    monkeypatch.setattr(ig, "_ensure", lambda cur: None)
    monkeypatch.setattr(ig, "_summary", lambda cur: ([dict(_LAYER)], []))
    monkeypatch.setattr(pu, "published_updates", lambda force=False: {"ok": True, "cards": []})
    monkeypatch.setattr(pu, "canon_values", lambda: {})
    monkeypatch.setattr(pu, "resolve_card_metrics",
                        lambda block, canon: {"ok": True, "cards": [{"title": "t"}], "withheld": []})
    # raising=False: on a tree without the helper the route still renders, so
    # the test fails on the missing JSON keys it exists for, not on setup.
    monkeypatch.setattr(cp, "headline_counts",
                        lambda: {"counts": dict(STUB_COUNTS), "provisional": False},
                        raising=False)

    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(ig.infra_growth_bp)
    with app.test_client() as c:
        yield c


def test_machine_door_carries_counts_as_of_and_recent_items(client):
    r = client.get("/api/v1/whats-new")
    assert r.status_code == 200
    d = r.get_json()
    # the four headline counts, verbatim from the canon reader
    assert d.get("counts") == STUB_COUNTS, (
        "/api/v1/whats-new does not publish the headline counts the /whats-new page shows")
    assert d.get("counts_source") == "/api/v1/canon/phrases"
    assert d.get("counts_provisional") is False
    # as_of: an explicit-UTC instant, the same one as generated_at
    as_of = d.get("as_of")
    assert as_of, "/api/v1/whats-new publishes no as_of"
    parsed = datetime.datetime.fromisoformat(as_of)
    assert parsed.utcoffset() == datetime.timedelta(0), f"as_of is not UTC: {as_of!r}"
    assert as_of == d.get("generated_at")
    # the recent items survive beside them
    assert isinstance(d.get("items"), list) and d["items"], "items[] missing"
    assert {i["category"] for i in d["items"]} >= {"Data-center deals"}
    assert d.get("platform") == [{"title": "t"}]


def test_unreadable_canon_is_null_counts_not_a_500(client, monkeypatch):
    import routes.canon_phrases as cp

    def boom():
        raise RuntimeError("canon down")

    monkeypatch.setattr(cp, "headline_counts", boom, raising=False)
    r = client.get("/api/v1/whats-new")
    assert r.status_code == 200
    d = r.get_json()
    assert "counts" in d and d["counts"] is None and d.get("counts_provisional") is True
    assert d["items"], "a canon failure must not blank the items that already work"


def _floors(cold=False, rejected=(), src="live"):
    return {"facilities": "24,500+", "deals": "1,600+", "markets": "300+", "countries": "170+",
            "_source": {"facilities": src, "deals": src, "markets": src, "countries": src},
            "_rejected": list(rejected), "_cold": cold}


def test_headline_counts_never_calls_resolve_canon(monkeypatch):
    import ai_surface_canon as asc
    from routes.canon_phrases import headline_counts

    # A RECORDER, not a raise: headline_counts() wraps its reads in try/except,
    # so an exception from a stray resolve_canon() call would be swallowed and
    # the test would pass over exactly the call it exists to forbid.
    calls = []
    monkeypatch.setattr(asc, "resolve_canon",
                        lambda: calls.append("resolve_canon") or {"tools_advertised": 91})
    monkeypatch.setattr(asc, "resolve_public_floors_cached", lambda: _floors())
    monkeypatch.setattr(asc, "resolve_tools_advertised_cached", lambda peek=False: 91)
    out = headline_counts()
    assert calls == [], "headline_counts() called resolve_canon() — a live probe on a request path"
    assert out == {"counts": STUB_COUNTS, "provisional": False}


def test_headline_counts_equals_what_the_canon_endpoint_serves(monkeypatch):
    """Same inputs, same four values as /api/v1/canon/phrases' own body."""
    import ai_surface_canon as asc
    import routes.canon_phrases as cp

    monkeypatch.setattr(asc, "resolve_public_floors_cached", lambda: _floors())
    monkeypatch.setattr(asc, "resolve_tools_advertised_cached", lambda peek=False: 91)
    monkeypatch.setattr(asc, "resolve_canon", lambda: {"tools_advertised": 91})
    body = cp._build_canon_body()
    assert cp.headline_counts()["counts"] == {k: body[k] for k in cp.HEADLINE_KEYS}


@pytest.mark.parametrize("floors", [
    _floors(cold=True, src="pinned"),
    _floors(rejected=["facilities=400<24400"]),
    _floors(src="pinned"),
], ids=["cold", "degraded", "all-pinned"])
def test_a_provisional_canon_is_labelled_provisional(monkeypatch, floors):
    import ai_surface_canon as asc
    from routes.canon_phrases import headline_counts

    monkeypatch.setattr(asc, "resolve_public_floors_cached", lambda: floors)
    monkeypatch.setattr(asc, "resolve_tools_advertised_cached", lambda peek=False: 91)
    out = headline_counts()
    assert out["counts"] is not None
    assert out["provisional"] is True, "a cold/degraded/pinned canon was published as live"
