"""2026-09-16 — a cache refresh that outlives its test failed other people's PRs.

ai_surface_canon.resolve_public_floors_cached() and
resolve_server_version_cached() answer from cache and, when it is cold, start a
daemon thread (public-floors-refresh, server-version-refresh) to refill it for
LATER. Right on a request path. In a test process nothing consumes the refill,
and the thread keeps resolving DNS after the test that started it has finished.

MEASURED, 8 runs of tests/test_agent_surfaces_withdrawn_dcgi.py followed by
tests/test_agent_tool_identifier_survives_markdown.py: the first file starts
both threads through routes/agents_md_fallback.py, and every run their lookups
landed while the second file's tests were running. The unit-tests step's
no-network hook reads PYTEST_CURRENT_TEST — main-thread state — so it stamped
them on that second file, which renders a Jinja template and reads routes/*.py
and opens no socket at all. The step then failed whichever PR was running, on
a file unchanged since 2026-09-05, naming a different one of its tests each
time. Two PRs a minute apart, #4635 at 01:25Z and #4644 at 01:26Z, went green
and red on the same unchanged file.

tests/conftest.py stops the fetch: off the main thread those refreshers clear
their in-flight flag and return. This file is the guard on that, and it asserts
BEHAVIOUR — whether resolve_canon() is reached — not that a wrapper is present.
A guard that only checked for the wrapper would pass on a wrapper that fetches.
"""
import threading

import pytest

ai_surface_canon = pytest.importorskip("ai_surface_canon")


@pytest.fixture
def reached(monkeypatch):
    """Count arrivals at the live layer, without going near a socket.

    Both refreshers bottom out here: _refresh_public_floors -> resolve_canon
    (through resolve_public_floors), _refresh_server_version -> the /mcp probe.
    """
    hits = []
    monkeypatch.setattr(ai_surface_canon, "resolve_canon",
                        lambda: hits.append("public-floors") or {"public": {}})
    monkeypatch.setattr(ai_surface_canon, "_mcp_server_version",
                        lambda *a, **k: hits.append("server-version") or "")
    return hits


def _in_a_thread(fn):
    t = threading.Thread(target=fn, name="probe-refresh")
    t.start()
    t.join(timeout=10)
    assert not t.is_alive(), "the refresher never returned off the main thread"


@pytest.mark.parametrize("refresher, flag, expected", [
    ("_refresh_public_floors", "_public_floors_refreshing", "public-floors"),
    ("_refresh_server_version", "_server_version_refreshing", "server-version"),
])
def test_a_refresher_reaches_on_the_main_thread_and_not_off_it(
        reached, refresher, flag, expected):
    """The whole fix in one assertion pair, stated as behaviour.

    Main thread: unchanged, because that call is bounded by the test making it —
    tests/test_recommend_serves_live_facility_floor.py drives
    _refresh_public_floors() directly and must keep working.
    Off the main thread: nothing reaches the live layer.
    """
    fn = getattr(ai_surface_canon, refresher)

    fn()
    assert reached == [expected], (
        f"{refresher}() no longer reaches the live layer on the main thread — "
        "a test that drives it synchronously is now asserting on nothing")

    _in_a_thread(fn)
    assert reached == [expected], (
        f"{refresher}() reached the live layer from a background thread. That "
        "fetch outlives the test that starts it, and the no-network hook stamps "
        "it on whatever test is running when it lands. See tests/conftest.py.")

    assert getattr(ai_surface_canon, flag) is False, (
        f"{flag} was left set. The module only starts a refresh when it is "
        "unset, so every later spawn in this process would be silently skipped "
        "— hiding the next background fetch instead of preventing it")


def test_the_cached_accessors_start_no_fetching_thread(reached):
    """The entry points the route modules actually call, on a cold cache.

    This is the path routes/agents_md_fallback.py took. A cold cache is the
    condition that spawns, so reset before asking.
    """
    with ai_surface_canon._public_floors_lock:
        ai_surface_canon._public_floors_cache["val"] = None
        ai_surface_canon._public_floors_cache["at"] = 0.0
        ai_surface_canon._public_floors_refreshing = False
    with ai_surface_canon._server_version_lock:
        ai_surface_canon._server_version_cache["val"] = None
        ai_surface_canon._server_version_cache["at"] = 0.0
        ai_surface_canon._server_version_refreshing = False

    before = {t.name for t in threading.enumerate()}
    assert ai_surface_canon.resolve_public_floors_cached()["facilities"]
    assert ai_surface_canon.resolve_server_version_cached()

    for t in threading.enumerate():
        if t.name in ("public-floors-refresh", "server-version-refresh") and t.name not in before:
            t.join(timeout=10)

    assert reached == [], (
        "a cached accessor started a background refresh that reached the live "
        f"layer: {reached}. Those threads outlive the test that starts them")
