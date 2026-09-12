"""Guard: a COLD REPLICA covers from the shared last-good canon, not the floor.

2026-09-12, second pass. The per-process cover (#4464) did not stop the
oscillation. Measured across the deploy that shipped it — 24 reads, one URL:

    t=48   cache=miss  cold=false  21,600+
    t=69   cache=miss  cold=true   21,500+
    8 cold bodies in all, and X-DC-Canon-Covering never once set.

A single process cannot do that: storing a warm body at t=48 arms the memo for
_CACHE_TTL_S, so t=69 would be a HIT. Two MISSES 21s apart, one warm and one
cold, means two PROCESSES with independent memos — replicas — and the cover
never fired because the cold one had no good body of its OWN. That case was
documented as a residual and is in fact the dominant one.

So the last-good body is published where every replica can read it. These tests
pin the behaviour, not the plumbing: the DB is stubbed, because what matters is
which body a caller is handed.
"""
import importlib
import sys

import pytest

sys.path.insert(0, ".")
cp = importlib.import_module("routes.canon_phrases")

# Captured BEFORE the autouse fixture replaces the module attributes. A test
# that reaches for cp._shared_put gets the stub and asserts nothing.
REAL_SHARED_PUT = cp._shared_put
REAL_SHARED_GET = cp._shared_get

GOOD = {
    "ok": True, "cold": False, "degraded": [],
    "facilities": "21,600+", "deals": "2,100+", "markets": "300+", "countries": "170+",
    "tools": 90,
    "value_source": {"facilities": "live", "deals": "live", "markets": "live", "countries": "live"},
    "source": "resolve_public_floors (live)",
}
PROVISIONAL = {
    "ok": True, "cold": True, "degraded": [],
    "facilities": "21,500+", "deals": "2,100+", "markets": "300+", "countries": "170+",
    "tools": 90,
    "value_source": {"facilities": "pinned", "deals": "pinned", "markets": "pinned", "countries": "pinned"},
    "source": "resolve_public_floors (cold: PINNED floors)",
}


@pytest.fixture(autouse=True)
def _isolated():
    """A genuinely cold replica: empty memo, and the shared store stubbed off
    unless a test opts in."""
    saved = dict(cp._cache)
    real_time, real_get, real_put = cp.time.time, cp._shared_get, cp._shared_put
    cp._cache.update({"at": 0.0, "body": None, "ttl": cp._CACHE_TTL_S,
                      "good_at": 0.0, "covering": False})
    cp._shared_get = lambda: (None, None)
    cp._shared_put = lambda body: None
    yield
    cp.time.time, cp._shared_get, cp._shared_put = real_time, real_get, real_put
    cp._cache.clear(); cp._cache.update(saved)


def _clock(t):
    cp.time.time = lambda: t


def test_fixtures_read_as_intended():
    assert cp._is_provisional(GOOD) is False
    assert cp._is_provisional(PROVISIONAL) is True


def test_a_cold_replica_covers_from_the_shared_store():
    """The measured case: no local good body, another replica has one."""
    cp._shared_get = lambda: (GOOD, 30.0)
    _clock(1000.0)
    body, _ = cp._cached_body(lambda: PROVISIONAL)
    assert body["facilities"] == "21,600+", (
        "a cold replica served its pinned floor while a good body was published "
        "and 30s old — this is the oscillation customers saw"
    )
    assert cp._cache["covering"] is True


def test_the_shared_cover_respects_the_grace_bound():
    cp._shared_get = lambda: (GOOD, cp._GOOD_BODY_GRACE_S + 1)
    _clock(2000.0)
    body, _ = cp._cached_body(lambda: PROVISIONAL)
    assert body["facilities"] == "21,500+", (
        "a shared body older than the grace still covered — a real canon outage "
        "would stay invisible across every replica at once, which is worse"
    )


def test_the_adopted_body_keeps_its_TRUE_age():
    """Adopting must not reset the clock. A body adopted at 800s old with a 900s
    grace has 100s of cover left, not a fresh 900."""
    cp._shared_get = lambda: (GOOD, 800.0)
    _clock(3000.0)
    assert cp._cached_body(lambda: PROVISIONAL)[0]["facilities"] == "21,600+"
    assert cp._cache["good_at"] == pytest.approx(3000.0 - 800.0), (
        "the adopted body was stamped as if it were new, which silently extends "
        "the grace window to 900s past adoption"
    )
    # Past its true expiry, the cover stops even though adoption was recent.
    cp._shared_get = lambda: (None, None)
    _clock(3000.0 + 100.0 + cp._PROVISIONAL_TTL_S + 2)
    assert cp._cached_body(lambda: PROVISIONAL)[0]["facilities"] == "21,500+"


def test_a_good_body_is_published_for_the_other_replicas():
    published = []
    cp._shared_put = lambda body: published.append(body)
    _clock(4000.0)
    cp._cached_body(lambda: GOOD)
    assert published and published[0]["facilities"] == "21,600+", (
        "nothing was published, so every other replica still has only its floor"
    )


def test_a_provisional_body_is_never_published(monkeypatch):
    """Publishing a floor would spread it to EVERY replica — the inverse defect,
    and the worse one.

    Uses REAL_SHARED_PUT, captured at import before the autouse fixture stubs
    the module attribute. Calling cp._shared_put here would call that stub and
    assert nothing — which is exactly what a first version of this test did.
    """
    reached = []
    monkeypatch.setattr(cp, "_ensure_shared_table",
                        lambda: reached.append("table") or False)
    REAL_SHARED_PUT(PROVISIONAL)
    assert reached == [], (
        "a provisional body got as far as the shared store; the guard that "
        "refuses to publish a floor is not firing"
    )


def test_the_publish_guard_is_not_vacuous(monkeypatch):
    """The control for the test above: a GOOD body must get past the same guard
    and reach the store. Without this, `reached == []` passes for a _shared_put
    that never publishes anything at all."""
    reached = []
    monkeypatch.setattr(cp, "_ensure_shared_table",
                        lambda: reached.append("table") or False)
    REAL_SHARED_PUT(GOOD)
    assert reached == ["table"], (
        "a good body did not reach the store either, so the previous test "
        "proves nothing about provisional bodies specifically"
    )


def test_a_broken_shared_store_changes_nothing():
    """If the store is unreachable the endpoint must behave exactly as it did
    before it existed — a cover is an improvement, never a dependency."""
    def boom():
        raise RuntimeError("no DATABASE_URL")
    cp._shared_get = boom
    _clock(6000.0)
    with pytest.raises(RuntimeError):
        cp._cached_body(lambda: PROVISIONAL)


def test_ddl_runs_on_the_blessed_cursor_not_a_pooled_one():
    """A pooled cursor DROPS CREATE TABLE silently (SKIP_DDL defaults on), which
    is how mcp_sessions stayed missing for three months. Pinned behaviourally:
    the real _ensure_shared_table must reach db_utils.ddl_cursor."""
    import contextlib
    import db_utils
    used = {"ddl_cursor": False}

    class _Cur:
        def execute(self, *a, **k): pass
        def fetchone(self): return (True,)

    @contextlib.contextmanager
    def fake_ddl_cursor():
        used["ddl_cursor"] = True
        yield _Cur()

    real = db_utils.ddl_cursor
    db_utils.ddl_cursor = fake_ddl_cursor
    cp._shared_ready = False
    try:
        assert cp._ensure_shared_table() is True
    finally:
        db_utils.ddl_cursor = real
        cp._shared_ready = False
    assert used["ddl_cursor"], (
        "_ensure_shared_table did not use db_utils.ddl_cursor — on a pooled "
        "cursor the CREATE TABLE is discarded with no error and no table"
    )
