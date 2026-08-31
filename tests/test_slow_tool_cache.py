"""Guards for routes/_slow_tool_cache.py.

The module's whole value is three promises, and each one is the kind that fails
silently if it regresses:

  1. equivalent inputs collapse to ONE cache entry (else the memo never hits),
  2. a failed response is NEVER stored (else one transient error is served for
     the whole TTL),
  3. everything is best-effort (else a Redis outage takes the tool down).

Each test below asserts the promise AND is written so that breaking the
implementation breaks the test — no test here passes against a stubbed-out
module. `test_storable_predicate_rejects_each_failure_shape` in particular walks
every rejection shape separately, so removing any single guard reddens a named
case rather than being masked by a sibling.

No network, no Redis, no Flask app: the decorator is exercised against a fake
`redis_cache` injected into sys.modules and a minimal request context.
"""

import json
import sys
import types

import pytest

flask = pytest.importorskip("flask")


# ── helpers ──────────────────────────────────────────────────────────

class _FakeRedis:
    """Stand-in for redis_cache with a real dict behind it, so a HIT is a
    genuine second-call-served-from-store and not an assertion about a mock."""

    def __init__(self, explode_on_get=False, explode_on_set=False):
        self.store = {}
        self.gets = 0
        self.sets = 0
        self.explode_on_get = explode_on_get
        self.explode_on_set = explode_on_set

    def cache_get(self, key):
        self.gets += 1
        if self.explode_on_get:
            raise RuntimeError("redis down")
        return self.store.get(key)

    def cache_set(self, key, data, ttl=300):
        self.sets += 1
        if self.explode_on_set:
            raise RuntimeError("redis down")
        self.store[key] = data


def _install(fake):
    mod = types.ModuleType("redis_cache")
    mod.cache_get = fake.cache_get
    mod.cache_set = fake.cache_set
    sys.modules["redis_cache"] = mod
    return mod


@pytest.fixture
def stc(monkeypatch):
    """Import the module under test fresh, with the kill switch off."""
    monkeypatch.delenv("DCHUB_SLOW_TOOL_CACHE", raising=False)
    sys.modules.pop("routes._slow_tool_cache", None)
    from routes import _slow_tool_cache as m
    return m


@pytest.fixture
def app():
    return flask.Flask(__name__)


# ── 1. key normalisation ─────────────────────────────────────────────

def test_norm_collapses_equivalent_coordinates(stc):
    """33.45, '33.45', '33.4500' and 33.450001 are the same site.

    If this regresses the cache still 'works' — it just never hits, which is
    invisible from the outside and exactly the bug that makes a memo useless."""
    assert stc._norm(33.45) == stc._norm("33.45") == stc._norm("33.4500")
    assert stc._norm(33.450001) == stc._norm(33.45)
    # and genuinely different sites must NOT collapse
    assert stc._norm(33.45) != stc._norm(33.46)
    # None and empty are the same request
    assert stc._norm(None) == stc._norm("") == ""
    # case-insensitive for non-numerics
    assert stc._norm("VA") == stc._norm("va")


def test_norm_rounds_to_declared_precision(stc):
    """Rounding is _COORD_DP, not an accident of float repr."""
    assert stc._COORD_DP == 3
    # 4th decimal is dropped; 3rd is kept
    assert stc._norm(1.23449) == stc._norm(1.2345) == "1.234"
    assert stc._norm(1.2355) != stc._norm(1.2345)


def test_key_order_follows_declaration_not_request_order(stc, app):
    """Two callers sending the same values in different query order share one
    entry. Keyed on request order instead, the cache would silently fragment."""
    with app.test_request_context("/x?state=VA&lat=1.0&lng=2.0"):
        a = stc._request_parts(("lat", "lng", "state"))
    with app.test_request_context("/x?lng=2.0&state=VA&lat=1.0"):
        b = stc._request_parts(("lat", "lng", "state"))
    assert a == b == ["lat=1.000", "lng=2.000", "state=va"]


# ── 2. only clean 200s are stored ────────────────────────────────────

@pytest.mark.parametrize("body,status,why", [
    # NOTE: these two cases must isolate ONE guard each. An earlier draft used
    # {"success": False, "error": "boom"} for the first, and the `error` guard
    # masked it — deleting the success:false guard entirely left the suite green.
    # Keep the shapes disjoint or the mutation test lies.
    ({"success": False, "detail": "boom"}, 200, "success:false must not be stored"),
    ({"error": "rate limited"}, 200, "an error key must not be stored"),
    ({"ok": True}, 500, "a 500 must not be stored"),
    ({"ok": True}, 404, "a 404 must not be stored"),
])
def test_storable_predicate_rejects_each_failure_shape(stc, app, body, status, why):
    """Each rejection shape gets its own case so that deleting ONE guard
    reddens a named test instead of hiding behind a sibling."""
    fake = _FakeRedis()
    _install(fake)

    @stc.cache_tool_response(ttl=60, prefix="t", arg_names=("lat",))
    def view():
        return flask.jsonify(body), status

    with app.test_request_context("/x?lat=1.0"):
        view()
    assert fake.store == {}, why


def test_successful_response_is_stored_and_then_served(stc, app):
    """The positive case — proves the negatives above are not passing merely
    because nothing is ever stored."""
    fake = _FakeRedis()
    _install(fake)
    calls = {"n": 0}

    @stc.cache_tool_response(ttl=60, prefix="t", arg_names=("lat",))
    def view():
        calls["n"] += 1
        return flask.jsonify({"score": 77})

    with app.test_request_context("/x?lat=1.0"):
        r1 = view()
    assert calls["n"] == 1
    assert len(fake.store) == 1
    assert r1.headers.get("X-Tool-Cache") == "MISS"

    with app.test_request_context("/x?lat=1.0"):
        r2 = view()
    assert calls["n"] == 1, "second identical call must not re-run the handler"
    assert r2.headers.get("X-Tool-Cache") == "HIT"
    assert json.loads(r2.get_data(as_text=True)) == {"score": 77}


def test_different_inputs_do_not_share_an_entry(stc, app):
    fake = _FakeRedis()
    _install(fake)
    calls = {"n": 0}

    @stc.cache_tool_response(ttl=60, prefix="t", arg_names=("lat",))
    def view():
        calls["n"] += 1
        return flask.jsonify({"n": calls["n"]})

    with app.test_request_context("/x?lat=1.0"):
        view()
    with app.test_request_context("/x?lat=9.0"):
        view()
    assert calls["n"] == 2
    assert len(fake.store) == 2


def test_auth_header_is_deliberately_ignored(stc, app):
    """The reason this module exists. redis_cache.cached_endpoint skips any
    request carrying Authorization/X-API-Key; these tools are @require_pro, so
    that behaviour would mean a 0% hit rate. Two different keys, same inputs,
    must share one entry."""
    fake = _FakeRedis()
    _install(fake)
    calls = {"n": 0}

    @stc.cache_tool_response(ttl=60, prefix="t", arg_names=("lat",))
    def view():
        calls["n"] += 1
        return flask.jsonify({"score": 1})

    with app.test_request_context("/x?lat=1.0", headers={"X-API-Key": "key-a"}):
        view()
    with app.test_request_context("/x?lat=1.0", headers={"Authorization": "Bearer b"}):
        view()
    assert calls["n"] == 1, "auth header must not fragment or bypass the cache"


# ── 3. best-effort ───────────────────────────────────────────────────

def test_redis_read_failure_falls_through_to_the_live_call(stc, app):
    fake = _FakeRedis(explode_on_get=True)
    _install(fake)

    @stc.cache_tool_response(ttl=60, prefix="t", arg_names=("lat",))
    def view():
        return flask.jsonify({"score": 5})

    with app.test_request_context("/x?lat=1.0"):
        resp = view()
    assert json.loads(resp.get_data(as_text=True)) == {"score": 5}


def test_redis_write_failure_does_not_break_the_response(stc, app):
    fake = _FakeRedis(explode_on_set=True)
    _install(fake)

    @stc.cache_tool_response(ttl=60, prefix="t", arg_names=("lat",))
    def view():
        return flask.jsonify({"score": 5})

    with app.test_request_context("/x?lat=1.0"):
        resp = view()
    assert resp.status_code == 200


def test_missing_redis_module_falls_through(stc, app, monkeypatch):
    """REDIS_URL unset / redis_cache absent must degrade to slow, not to 500."""
    sys.modules.pop("redis_cache", None)
    monkeypatch.setitem(sys.modules, "redis_cache", None)

    @stc.cache_tool_response(ttl=60, prefix="t", arg_names=("lat",))
    def view():
        return flask.jsonify({"score": 5})

    with app.test_request_context("/x?lat=1.0"):
        resp = view()
    assert resp.status_code == 200


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "OFF"])
def test_kill_switch_disables_the_memo(stc, app, monkeypatch, val):
    monkeypatch.setenv("DCHUB_SLOW_TOOL_CACHE", val)
    fake = _FakeRedis()
    _install(fake)
    calls = {"n": 0}

    @stc.cache_tool_response(ttl=60, prefix="t", arg_names=("lat",))
    def view():
        calls["n"] += 1
        return flask.jsonify({"score": 1})

    with app.test_request_context("/x?lat=1.0"):
        view()
    with app.test_request_context("/x?lat=1.0"):
        view()
    assert calls["n"] == 2, "kill switch must bypass the memo entirely"
    assert fake.gets == 0 and fake.sets == 0


def test_default_is_enabled(stc, monkeypatch):
    monkeypatch.delenv("DCHUB_SLOW_TOOL_CACHE", raising=False)
    assert stc._enabled() is True
    monkeypatch.setenv("DCHUB_SLOW_TOOL_CACHE", "1")
    assert stc._enabled() is True


# ── 4. the wiring is actually applied to the two slow tools ──────────
# Without this, the module could be perfect and unused — the failure mode the
# audit found everywhere else (a guard that exists and never fires).

def test_composite_score_route_carries_the_memo():
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1] / "site_planner.py"
    text = src.read_text()
    i = text.find("def site_planner_composite_score(")
    assert i > 0, "handler not found — did it get renamed?"
    window = text[max(0, i - 1400):i]
    assert "cache_tool_response(" in window, \
        "composite-score lost its memo — p50 was 11.5 s without it"
    assert "prefix='composite_score'" in window


def test_refined_queue_route_carries_the_memo():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "routes" / "interconnection_queues.py")
    text = src.read_text()
    i = text.find("def api_refined_queue(")
    assert i > 0, "handler not found — did it get renamed?"
    window = text[max(0, i - 1400):i]
    assert "cache_tool_response(" in window, \
        "refined-queue lost its memo — p50 was 11.0 s without it"
    assert "prefix='refined_queue'" in window
