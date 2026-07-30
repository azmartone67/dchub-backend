"""Regression guard: /radar served its edition NUMBER and its printed DATE from
two different clocks (r-radarclock, 2026-07-29).

THE DEFECT. routes/radar.py recomputed the edition slot on every request
(_today_slug / _edition_tokens) but printed a date that came out of the 15-minute
cached data core: radar.py `_build_core` stamps `retrieved_at` from its own
now(), radar_templates.normalize derives `retrieved_date = retrieved_at[:10]`,
and the templates print that next to "Edition Nº NNN". So for up to 15 minutes
after 00:00 UTC — and INDEFINITELY whenever the background rebuild kept failing,
because `_refresh_core_async` swallowed every exception and `_pull_core` then
returned the stale copy — the page paired a NEW edition number with the PREVIOUS
day's date. The owner saw "Edition Nº 004 · 2026-07-29"; 2026-07-29 UTC is slot
003, so that pairing is unreachable from any single consistent clock.

SECOND DEFECT. The cycle was `day_of_year % 4`. 365 % 4 == 1, so the phase jumps
every Jan 1: 2026-12-31 and 2027-01-01 both resolved to `siteselect` and one
edition was skipped. Same for the `% 7` featured-ISO rotation.

These tests are BEHAVIOURAL — they drive the real render/JSON/rotation code with
a frozen clock and a controlled cache, and assert on the produced output. They do
not grep the source, which a comment would satisfy.

No import of main.py. `routes.radar` is imported directly (as
tests/test_webmcp_pages.py already does); the DB and the ~10 loopback calls are
never reached because `_load_core_db` / `_save_core_db` / `_build_core` are
stubbed in every test.
"""
import datetime as _real_dt
import re
import time
import types

import pytest


# ── frozen clock ─────────────────────────────────────────────────────────────
def _freeze(monkeypatch, radar, when):
    """Replace the `dt` module object radar.py reads through, so every
    dt.datetime.now(dt.timezone.utc) in the module returns `when`.

    Patching the module attribute (not datetime globally) works identically
    against the pre-fix and post-fix source, which is what makes the
    patched-vs-unpatched comparison meaningful."""
    class _FrozenDatetime(_real_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return when if tz is not None else when.replace(tzinfo=None)

    monkeypatch.setattr(radar, "dt", types.SimpleNamespace(
        datetime=_FrozenDatetime,
        timezone=_real_dt.timezone,
        date=_real_dt.date,
        timedelta=_real_dt.timedelta,
    ))


def _utc(y, m, d, hh=0, mm=0):
    return _real_dt.datetime(y, m, d, hh, mm, tzinfo=_real_dt.timezone.utc)


def _core_at(stamp: _real_dt.datetime) -> dict:
    """A minimal but complete data core stamped at `stamp`.

    normalize() fills every US ISO row whether or not `grids` carries it, so an
    empty grids list still yields the 7-row `isos` list the templates loop over.
    """
    return {
        "retrieved_at": stamp.isoformat(timespec="seconds"),
        "depth": "full",
        "citation": {"cite_as": "DC Hub, dchub.cloud", "license": "CC-BY-4.0"},
        "scoreboard": {"us_interconnection_queue_gw": 1737.3, "grids": []},
        "ashburn": {"demand_mw": 16295.57, "lmp_rt_usd_mwh": 36.94,
                    "lmp_congestion_usd_mwh": 3.81},
        "markets": {"results": []},
    }


@pytest.fixture
def radar():
    """routes.radar with its process-level core cache isolated per test."""
    import routes.radar as _radar
    saved_cache = dict(_radar._CORE_CACHE)
    saved_err = dict(getattr(_radar, "_CORE_LAST_ERROR", {}) or {})
    _radar._CORE_CACHE.update(ts=0.0, core=None)
    _radar._CORE_REFRESHING = False
    if hasattr(_radar, "_CORE_LAST_ERROR"):
        _radar._CORE_LAST_ERROR.update(at=0.0, type="", msg="")
    try:
        yield _radar
    finally:
        _radar._CORE_CACHE.clear()
        _radar._CORE_CACHE.update(saved_cache)
        _radar._CORE_REFRESHING = False
        if hasattr(_radar, "_CORE_LAST_ERROR") and saved_err:
            _radar._CORE_LAST_ERROR.clear()
            _radar._CORE_LAST_ERROR.update(saved_err)


def _stub_core_io(monkeypatch, radar, cached_core, cached_age_s, builder):
    """Pin the shared cache to `cached_core` (aged `cached_age_s` seconds) and
    make a rebuild run `builder` instead of ~10 loopback GETs + a DB read."""
    monkeypatch.setattr(radar, "_load_core_db",
                        lambda: (time.time() - cached_age_s, cached_core))
    monkeypatch.setattr(radar, "_save_core_db", lambda ts, core: None)
    monkeypatch.setattr(radar, "_build_core", lambda *a, **k: builder())


def _slot_by_doy(radar, d: _real_dt.date) -> dict:
    """The edition the SHIPPED-BEFORE-THIS-FIX rule (`day_of_year % 4`) selects.
    Used to prove the fix does not move any 2026 edition."""
    return radar.EDITIONS[d.timetuple().tm_yday % len(radar.EDITIONS)]


# ── 1 · the split-brain pairing ──────────────────────────────────────────────
def test_printed_date_and_edition_number_agree_just_after_utc_midnight(
        monkeypatch, radar):
    """Three minutes past 00:00 UTC with a 1-hour-old core stamped YESTERDAY.

    Pre-fix: the core is stale on TTL only, so the previous day's copy is served
    and the page prints 2026-07-29 beside the freshly-computed Nº 004.
    Post-fix: a UTC date crossing is itself staleness, so the core is rebuilt and
    the page prints one date, 2026-07-30, matching Nº 004."""
    now = _utc(2026, 7, 30, 0, 3)
    yesterday = _utc(2026, 7, 29, 23, 50)
    _freeze(monkeypatch, radar, now)
    _stub_core_io(monkeypatch, radar, _core_at(yesterday), 3600,
                  lambda: _core_at(now))

    slug = radar._today_slug()
    html = radar._render_edition(slug, "tease")

    dates = set(re.findall(r"20\d\d-\d\d-\d\d", html))
    assert dates == {"2026-07-30"}, (
        "page must print exactly one date, today's; got %s" % sorted(dates))

    nos = set(re.findall(r"Edition N&ordm; (\d{3})", html))
    expected = "%03d" % _slot_by_doy(radar, now.date())["no"]
    assert nos == {expected}, (
        "printed edition number %s does not match the slot for the printed "
        "date (%s)" % (sorted(nos), expected))


def test_within_day_ttl_expiry_still_serves_stale_while_revalidating(
        monkeypatch, radar):
    """The date fix must NOT turn every TTL expiry into a blocking rebuild —
    stale-while-revalidate is why page views render warm. Same UTC date, 20 min
    old: serve the cached copy, do not build inline."""
    now = _utc(2026, 7, 30, 12, 0)
    earlier = _utc(2026, 7, 30, 11, 40)
    built = []
    _freeze(monkeypatch, radar, now)
    _stub_core_io(monkeypatch, radar, _core_at(earlier), 1200,
                  lambda: (built.append(1), _core_at(now))[1])
    monkeypatch.setattr(radar, "_refresh_core_async", lambda: None)

    out = radar._pull_core()          # reads the frozen clock itself
    core = out[0] if isinstance(out, tuple) else out

    assert core["retrieved_at"].startswith("2026-07-30")
    assert built == [], "TTL expiry inside the same UTC day must not build inline"


def test_date_cross_rebuild_happens_once_not_per_request(monkeypatch, radar):
    """The inline rebuild at the UTC boundary is ~10 loopback GETs. It must be
    paid ONCE and then cached, not on every request for the rest of the day."""
    now = _utc(2026, 7, 30, 0, 3)
    yesterday = _utc(2026, 7, 29, 23, 50)
    built = []
    _freeze(monkeypatch, radar, now)
    monkeypatch.setattr(radar, "_save_core_db", lambda ts, core: None)
    monkeypatch.setattr(radar, "_build_core",
                        lambda *a, **k: (built.append(1), _core_at(now))[1])
    # the shared copy stays yesterday's — only the in-process cache can dedupe
    monkeypatch.setattr(radar, "_load_core_db",
                        lambda: (time.time() - 3600, _core_at(yesterday)))

    for _ in range(3):
        out = radar._pull_core()
        core = out[0] if isinstance(out, tuple) else out
        assert core["retrieved_at"].startswith("2026-07-30")
    assert len(built) == 1, "rebuilt %d times; expected exactly 1" % len(built)


# ── 2 · a permanently-failing rebuild must be VISIBLE ────────────────────────
def test_unrebuildable_core_is_disclosed_not_silently_published(
        monkeypatch, radar, caplog):
    """Every rebuild raises, across a UTC date boundary — the exact shape that
    pinned yesterday's date on the page forever with zero signal.

    The page must disclose BOTH dates (so a reader can see the figures are not
    today's) and the failure must reach the log with its exception type."""
    now = _utc(2026, 7, 30, 6, 0)
    yesterday = _utc(2026, 7, 29, 23, 50)
    _freeze(monkeypatch, radar, now)

    def _boom():
        raise RuntimeError("upstream grid endpoint down")

    _stub_core_io(monkeypatch, radar, _core_at(yesterday), 7200, _boom)

    with caplog.at_level("WARNING"):
        html = radar._render_edition(radar._today_slug(), "tease")

    dates = set(re.findall(r"20\d\d-\d\d-\d\d", html))
    assert "2026-07-30" in dates and "2026-07-29" in dates, (
        "a core that cannot be rebuilt must be published WITH the discrepancy "
        "visible (edition date AND the date the figures carry); got %s"
        % sorted(dates))

    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("RuntimeError" in m for m in warnings), (
        "the rebuild failure must be logged with its exception type, not "
        "swallowed; warnings seen: %s" % warnings)


# ── 3 · the cycle must not skip or repeat across the year boundary ───────────
def test_edition_cycle_has_no_repeat_or_skip_across_new_year(monkeypatch, radar):
    """`doy % 4` jumps phase on Jan 1 (365 % 4 == 1): 2026-12-31 and 2027-01-01
    both resolved to `siteselect`, and one edition was skipped."""
    days = [_real_dt.date(2026, 12, 29) + _real_dt.timedelta(n) for n in range(8)]
    seq = []
    for d in days:
        _freeze(monkeypatch, radar, _utc(d.year, d.month, d.day, 9, 0))
        seq.append(radar._today_slug())

    pairs = list(zip(days, seq))
    for (d0, s0), (d1, s1) in zip(pairs, pairs[1:]):
        assert s0 != s1, ("edition repeated on consecutive days: %s and %s both "
                          "served %r — sequence %s" % (d0, d1, s0, pairs))
    n = len(radar.EDITIONS)
    for i in range(len(seq) - n + 1):
        window = seq[i:i + n]
        assert len(set(window)) == n, (
            "a %d-day window skipped an edition: %s" % (n, list(zip(days[i:i + n], window))))


def test_featured_iso_rotation_does_not_repeat_across_new_year(monkeypatch, radar):
    """Same phase-jump bug in the free-unlocked ISO row (`doy % 7`)."""
    days = [_real_dt.date(2026, 12, 30) + _real_dt.timedelta(n) for n in range(4)]
    seen = []
    for d in days:
        when = _utc(d.year, d.month, d.day, 9, 0)
        _freeze(monkeypatch, radar, when)
        import routes.radar_templates as T
        data = T.normalize(_core_at(when))
        seen.append(radar._edition_tokens("capital", data)["featured_iso"])
    for (d0, a), (d1, b) in zip(zip(days, seen), list(zip(days, seen))[1:]):
        assert a != b, ("featured ISO repeated on consecutive days %s/%s: %r "
                        "— sequence %s" % (d0, d1, a, list(zip(days, seen))))


def test_fix_does_not_move_any_2026_edition(monkeypatch, radar):
    """The rotation is re-anchored, not re-phased: every date in the CURRENT
    publication year must resolve to the same edition it already served, so the
    fix cannot itself look like a skipped day."""
    moved = []
    d = _real_dt.date(2026, 1, 1)
    while d.year == 2026:
        _freeze(monkeypatch, radar, _utc(d.year, d.month, d.day, 9, 0))
        got = radar._today_slug()
        want = _slot_by_doy(radar, d)["slug"]
        if got != want:
            moved.append((d.isoformat(), want, got))
        d += _real_dt.timedelta(1)
    assert moved == [], "these 2026 dates changed edition: %s" % moved[:10]


# ── 4 · the machine-readable feed must not conflate the two ──────────────────
def test_teaser_json_separates_edition_date_from_retrieval_stamp(
        monkeypatch, radar):
    """Agents consume /radar/<slug>.json. It carried only `retrieved_at`, so a
    stale core made the feed's implied "today" wrong with nothing to detect it
    by. It must publish the edition's own date and a staleness flag."""
    now = _utc(2026, 7, 30, 0, 3)
    yesterday = _utc(2026, 7, 29, 23, 50)
    _freeze(monkeypatch, radar, now)
    _stub_core_io(monkeypatch, radar, _core_at(yesterday), 3600,
                  lambda: _core_at(now))

    feed = radar._teaser_json(radar._today_slug())

    assert feed.get("edition_date") == "2026-07-30", (
        "feed must state the UTC date its cycle_no belongs to; got %r"
        % feed.get("edition_date"))
    assert "stale" in feed, "feed must expose whether the core is stale"
    assert feed["retrieved_at"].startswith("2026-07-30")


# ── 5 · MUST-FAIL control: proves this file actually RAN ─────────────────────
# A conftest-level exit yields rc 0 with ZERO tests collected, which renders as
# an ordinary green job. If this does not report `xfailed`, nothing above ran.
@pytest.mark.xfail(strict=True, reason="control: this suite must be executing")
def test_control_must_fail():
    assert False, "control assertion — expected to fail, reported as xfailed"
