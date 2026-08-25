"""publisher-status must answer "is this platform dark?" from ANY replica.

★ THE GAP, measured 2026-08-25. A public read of
/api/v1/dchub-media/publisher-status returned, for all three platforms:

    {"attempts_24h": 0, "boot_disabled_reason": "not publish leader"}

while LinkedIn was publishing 2-3 posts a day. Every section of that payload
described THE PROCESS THAT ANSWERED — a web replica, which never publishes
because the worker holds the leader lock. The honest reading was "we cannot
tell from outside whether Twitter and Bluesky are dark": an observability gap,
NOT evidence of silence.

The publish record itself is durable and replica-independent — rows in
social_media_posts — and content_publisher._DEADMAN_SUCCESS_SQL already knows
how to ask for it per platform. `last_publish` reuses that map so the 72h
watchdog and this surface cannot disagree about what counts as a publish.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import routes.publisher_status as ps                              # noqa: E402


@pytest.fixture(autouse=True)
def _clear_cache():
    ps._last_publish_cache["at"] = 0.0
    ps._last_publish_cache["value"] = None
    yield
    ps._last_publish_cache["at"] = 0.0
    ps._last_publish_cache["value"] = None


def test_all_three_platforms_are_reported(monkeypatch):
    """★ Twitter and Bluesky are the whole point. LinkedIn was already
    visible through its own activity; the other two were not."""
    monkeypatch.setattr(ps, "_last_publish_uncached",
                        lambda: {p: {"status": "never_published"} for p in ps._PLATFORMS})
    out = ps._last_publish()
    assert set(out["by_platform"]) == {"linkedin", "twitter", "bluesky"}


def test_never_published_is_not_reported_as_silent(monkeypatch):
    """★★★ A platform that has NEVER published has no silence to measure.
    Reporting an infinite age would read as a loop that broke, and would send
    an operator to debug a publisher that was simply never used."""
    import datetime as dt
    monkeypatch.setattr(ps, "_PLATFORMS", ("linkedin", "twitter"))

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
    class _Conn:
        def cursor(self): return _Cur()
        def close(self): pass

    monkeypatch.setitem(sys.modules, "psycopg2",
                        type("m", (), {"connect": staticmethod(lambda dsn: _Conn())}))
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    import content_publisher as cp
    monkeypatch.setattr(cp, "_deadman_last_db_success",
                        lambda cur, plat: (dt.datetime.now(dt.timezone.utc)
                                           if plat == "linkedin" else None))
    out = ps._last_publish_uncached()
    assert out["linkedin"]["status"] == "publishing"
    assert out["twitter"]["status"] == "never_published"
    assert out["twitter"]["age_hours"] is None, \
        "a never-published platform was given an age — that reads as a broken loop"


def test_a_db_outage_reports_a_note_not_a_dark_platform(monkeypatch):
    """★ FAIL-OPEN, and specifically fail-HONEST. An unreadable publish record
    must never be rendered as 'this platform published nothing' — that is the
    false negative that sends an operator to fix a working publisher."""
    def boom():
        raise RuntimeError("neon is down")
    monkeypatch.setattr(ps, "_last_publish_uncached",
                        lambda: {"note": "publish record unavailable (RuntimeError)"})
    out = ps._last_publish()
    assert "note" in out["by_platform"]
    assert "never_published" not in str(out["by_platform"])


def test_a_dead_database_yields_a_note_not_an_exception(monkeypatch):
    """★ Driven for real: psycopg2.connect raises, and the REAL function is
    called. This is a PUBLIC endpoint — a Neon hiccup must cost one section,
    not the whole surface."""
    def _boom(dsn):
        raise RuntimeError("neon is down")
    monkeypatch.setitem(sys.modules, "psycopg2",
                        type("m", (), {"connect": staticmethod(_boom)}))
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    out = ps._last_publish_uncached()
    assert "note" in out and "RuntimeError" in out["note"]


def test_the_public_wrapper_cannot_500_the_endpoint(monkeypatch):
    """★ Belt and braces. Even if the inner read finds a way to raise, the
    section returns a note — four other sections of this payload predate it
    and must not become collateral."""
    def boom():
        raise RuntimeError("anything at all")
    monkeypatch.setattr(ps, "_last_publish_uncached", boom)
    out = ps._last_publish()
    assert "note" in out["by_platform"]
    assert out["cache_age_s"] is None, "a failed read reported a cache age"


def test_the_cache_reports_its_own_age(monkeypatch):
    """★ A cold cache returning a dict is not evidence of freshness. Without
    cache_age_s an operator cannot tell a live read from a minute-old one."""
    calls = []
    monkeypatch.setattr(ps, "_last_publish_uncached",
                        lambda: calls.append(1) or {"linkedin": {"status": "publishing"}})
    first = ps._last_publish()
    assert first["cache_age_s"] == 0.0
    second = ps._last_publish()
    assert len(calls) == 1, "the cache did not hold — one read per request"
    assert second["cache_age_s"] >= 0.0


def test_the_cache_expires(monkeypatch):
    calls = []
    monkeypatch.setattr(ps, "_last_publish_uncached",
                        lambda: calls.append(1) or {"linkedin": {"status": "publishing"}})
    ps._last_publish()
    ps._last_publish_cache["at"] -= (ps._LAST_PUBLISH_TTL_S + 1)
    ps._last_publish()
    assert len(calls) == 2, "the cache never expired — the surface would go stale"


def test_it_reuses_the_watchdog_definition_of_a_publish():
    """★★★ ONE definition of 'published'. A second query here would drift from
    the 72h watchdog, and the two surfaces would disagree about whether a
    platform is dark — which is worse than neither existing."""
    import inspect
    src = inspect.getsource(ps._last_publish_uncached)
    assert "_DEADMAN_SUCCESS_SQL" in src
    assert "_deadman_last_db_success" in src
    assert "SELECT" not in src.upper().replace("SELECT 1 FROM", ""), \
        "publisher_status wrote its own publish query instead of reusing the watchdog's"


def test_the_payload_says_which_section_to_trust():
    """The in-memory sections are honest about THIS process and misleading
    about the system. The payload has to say so, or the next reader repeats
    the 2026-08-25 mistake."""
    import inspect
    src = inspect.getsource(ps.publisher_status)
    assert "reading_note" in src
    assert "last_publish" in src
