"""Guard for the reach rollup week pick — routes/ai_reach.py (2026-08-20).

★ THE DEFECT THIS GUARD EXISTS TO RETIRE

/api/v1/ai/reach published distinct_agents_7d from

    agents = max(int(r["distinct_external_ips"]) for r in rolled)

over the last TWO reach_weekly rows. The stated intent was sound — a
Monday-morning partial week must not dip the metric to an artificial low — but
max() is the wrong instrument for it. max() does not merely ignore a partial
week; it ignores EVERY decline, because the published number latches to
whichever of the two weeks is higher and stays there until that week ages out
of the window. A growth metric that cannot go down is not a measurement.

It went wrong in exactly that way. Live 2026-08-19 the endpoint served 72 — the
week of 2026-08-10, measured BEFORE dchub-mcp-server#202 (2026-08-18 06:31Z)
removed DC Hub's own GitHub Actions from is_real_external. GH Actions were
72.1% of agents (49/68) in the 7d before that correction. In the SAME payload
the canonical real_agents_7d read 47 and the post-correction week read 12, and
nothing in the JSON said which week the 72 was for.

Two fixes, both guarded here:
  * name the week — the latest COMPLETE one — and take every published field
    from that single row (_latest_complete);
  * flag it when a correction has since withdrawn the population it counted
    (_mark_superseded), reusing the ONE correction registry in weekly_series
    rather than restating those timestamps here.

Pure functions: no DB, no network, no Flask app.
"""
import contextlib
import datetime as _dt
import types as _types

_SRC = open("routes/ai_reach.py", encoding="utf-8").read()
_SLICE = _SRC[_SRC.index("def _as_date"):_SRC.index("def _attach_canonical_7d")]


# ── the clock, pinned (2026-08-24) ───────────────────────────────────────────
# ★ WHY THE FIXTURE NO LONGER READS THE WALL CLOCK.
#
# _latest_complete() reads date.today() ITSELF. Anchoring the fixture to a
# SECOND read of the clock is therefore not a stronger test, it is a race:
# build the rows on Sunday, assert on Monday, and the two reads are a whole
# ISO week apart. CI run 32675112379 crossed 00:00Z mid-run on 2026-08-24 and
# four tests here went red —
#
#     assert datetime.date(2026, 8, 17) == datetime.date(2026, 8, 10)
#
# with nothing shipped broken and the whole file green again on the rerun.
# That is the worst failure a required check can have: `unit-tests` gates main,
# so every PR whose ~19-minute run straddles midnight is blocked on a red job
# that means nothing — which trains people to re-run red jobs without reading
# them, the exact habit CLAUDE.md warns about.
#
# ★ It reproduces only where the LOCAL day rolls over: date.today() is local,
# so the same commit was red in CI and green on a US laptop for hours.
#
# So the clock the shipped code sees is injected, and the fixture rows AND the
# expected week both come from that one pinned value. _ASOF is the day the pick
# was written, which also makes the fixture the live payload the docstring
# above is about: the 2026-08-10 week that published 72 beside the 2026-08-17
# week that had not finished happening.
#
# Pinning does not retire the boundary: it is asserted on EVERY day of the week
# (test_the_pick_survives_the_rollover_that_broke_ci, which walks the clock
# across the Monday tick that turned CI red), and the shipped clock read still
# runs under the real date in the margin tests further down.
_ASOF = _dt.date(2026, 8, 20)                    # a Thursday — mid-week
_CLOCK = {"today": _ASOF}


class _DateMeta(type):
    """Everything except today() falls through to the real datetime.date.

    isinstance() included: _as_date branches on it, and a shim that answered
    that question its own way would send a plain date down the string path —
    the tests would exercise a branch production never takes.
    """

    def __instancecheck__(cls, obj):
        return isinstance(obj, _dt.date)

    def __getattr__(cls, name):
        return getattr(_dt.date, name)


class _PinnedDate(metaclass=_DateMeta):
    @classmethod
    def today(cls):
        return _CLOCK["today"]


@contextlib.contextmanager
def _at(day):
    """Run the shipped code as if today were `day`."""
    prev = _CLOCK["today"]
    _CLOCK["today"] = day
    try:
        yield
    finally:
        _CLOCK["today"] = prev


def _load(dt_module):
    ns = {"_dt": dt_module}
    exec(_SLICE, ns)
    return ns


_NS = _load(_types.SimpleNamespace(date=_PinnedDate, datetime=_dt.datetime,
                                   timedelta=_dt.timedelta))
_LIVE = _load(_dt)                               # the real clock — margin tests

_as_date = _NS["_as_date"]
_latest_complete = _NS["_latest_complete"]
_mark_superseded = _NS["_mark_superseded"]

_THIS_MONDAY = _ASOF - _dt.timedelta(days=_ASOF.weekday())    # 08-17, in progress
_LAST_MONDAY = _THIS_MONDAY - _dt.timedelta(weeks=1)          # 08-10, complete


def _row(week_start, ips, requests=0, per_platform=None):
    return {"week_start": week_start, "distinct_external_ips": ips,
            "requests": requests, "distinct_platforms": 1,
            "per_platform": per_platform or []}


# ── the week pick ────────────────────────────────────────────────────────────

def test_picks_the_complete_week_not_the_partial_one():
    """rolled arrives week_start DESC: [current partial, latest complete]."""
    rolled = [_row(_THIS_MONDAY, 12, 735), _row(_LAST_MONDAY, 72, 2100)]
    assert _latest_complete(rolled)["week_start"] == _LAST_MONDAY


def test_a_higher_partial_week_does_not_win():
    """★ THE REGRESSION — the shape max() got wrong.

    When the in-progress week is already ABOVE the complete one, max() would
    publish the partial figure and the metric would report a week that has not
    finished happening. This is also the branch that let a decline be hidden:
    under max() the number can only ever be the larger of the two.
    """
    rolled = [_row(_THIS_MONDAY, 900, 9999), _row(_LAST_MONDAY, 18, 800)]
    picked = _latest_complete(rolled)
    assert picked["week_start"] == _LAST_MONDAY
    assert picked["distinct_external_ips"] == 18
    assert max(r["distinct_external_ips"] for r in rolled) == 900, (
        "the old max() would have published the partial week's 900")


def test_the_metric_can_now_decline():
    """A falling series must actually fall — the whole point of the fix."""
    high = [_row(_THIS_MONDAY, 5), _row(_LAST_MONDAY, 80)]
    low = [_row(_THIS_MONDAY, 5), _row(_LAST_MONDAY, 20)]
    assert _latest_complete(high)["distinct_external_ips"] == 80
    assert _latest_complete(low)["distinct_external_ips"] == 20, (
        "a lower complete week must be published as lower")


def test_partial_week_only_falls_back_instead_of_crashing():
    """Cold rollup table on a Monday has exactly one, partial, row."""
    rolled = [_row(_THIS_MONDAY, 12, 735)]
    assert _latest_complete(rolled)["week_start"] == _THIS_MONDAY


def test_week_start_as_text_is_accepted():
    """reach_weekly.week_start is DATE, but a TEXT column must not 500 us."""
    rolled = [_row(_THIS_MONDAY.isoformat(), 12),
              _row(_LAST_MONDAY.isoformat(), 72)]
    assert _latest_complete(rolled)["distinct_external_ips"] == 72


def test_unparseable_week_start_is_skipped_not_fatal():
    rolled = [_row("garbage", 999), _row(_LAST_MONDAY, 72)]
    assert _latest_complete(rolled)["distinct_external_ips"] == 72


# ── the pin itself ───────────────────────────────────────────────────────────

def test_the_pin_still_puts_one_row_in_each_kind_of_week():
    """★ THE ANCHOR. A pinned date is only worth what it still MEANS.

    _THIS_MONDAY and _LAST_MONDAY are derived from _ASOF, so no bump can put
    the arithmetic out of step — but the SHIPPED notion of "the week that has
    not finished" can move (a Sunday-start week, an ISO week number, a UTC
    read), and if it did, these two constants would quietly stop meaning
    "partial" and "complete" while every test above went on passing against a
    scenario that no longer exists.

    So the property is asserted through the shipped pick, one constant at a
    time, each against a week old enough to be complete under any definition
    of the boundary. A boundary that moves for only PART of the week is caught
    by the day-by-day sweep in the next test, not here.
    """
    old = _dt.date(2026, 1, 5)
    assert _latest_complete([_row(_THIS_MONDAY, 1), _row(old, 2)])["week_start"] == old, (
        "_THIS_MONDAY is no longer classified as the IN-PROGRESS week, so the "
        "tests above are no longer testing the partial/complete split")
    assert _latest_complete([_row(_LAST_MONDAY, 1), _row(old, 2)])["week_start"] == _LAST_MONDAY, (
        "_LAST_MONDAY is no longer classified as a COMPLETE week")
    assert (_THIS_MONDAY - _LAST_MONDAY).days == 7, (
        "adjacent weeks — the pick must be decided by completeness, not by a gap")
    assert _LAST_MONDAY == _dt.date(2026, 8, 10), (
        "the fixture no longer stands on the live case this file documents: the "
        "2026-08-10 week that published 72 before dchub-mcp-server#202 withdrew "
        "the population it counted — the same week the supersession tests below "
        "name. Re-pin _ASOF only with that story carried across")


def test_the_pin_is_a_constant_and_not_another_clock_read():
    """★ WHY THE VALUE IS SPELLED OUT AND NOT COMPUTED.

    Injection alone already retires the rollover — one read, and the fixture
    and the function share it. But `_ASOF = date.today()` would build a
    DIFFERENT scenario every day: green everywhere, reproducible nowhere, and
    a failure nobody can replay without knowing which day it ran on. The
    definition is what has to be checked, because a value assertion cannot
    tell a constant from today's date (it would agree with both, one week a
    year).
    """
    own = open(__file__, encoding="utf-8").read()
    lines = [l for l in own.splitlines() if l.startswith("_ASOF =")]
    assert len(lines) == 1, (
        "no single _ASOF definition to check — this fence is blind. If the pin "
        "was renamed, re-point it here; do NOT delete the assertion")
    line = lines[0]
    assert "today(" not in line and "now(" not in line, (
        "_ASOF reads the clock: %s — pin a date instead, and update the "
        "scenario the anchor above names" % line)


def test_the_pick_survives_the_rollover_that_broke_ci():
    """★ THE REGRESSION, as a test instead of a rerun.

    The same two rows, judged on every day of two weeks. Mon 08-17..Sun 08-23
    must publish 08-10; the moment the clock ticks into Mon 08-24 the complete
    week becomes 08-17 itself. _latest_complete() reads a DATE, so those two
    adjacent days ARE the two sides of midnight — the step that turned this
    file red at 00:00Z with nothing shipped changed is now asserted on both
    sides of it, rather than left to whichever minute CI happens to start.
    """
    rolled = [_row(_THIS_MONDAY, 12, 735), _row(_LAST_MONDAY, 72, 2100)]
    for i in range(7):                                  # Mon 08-17 .. Sun 08-23
        day = _THIS_MONDAY + _dt.timedelta(days=i)
        with _at(day):
            assert _latest_complete(rolled)["week_start"] == _LAST_MONDAY, day
    for i in range(7):                                  # Mon 08-24 .. Sun 08-30
        day = _THIS_MONDAY + _dt.timedelta(days=7 + i)
        with _at(day):
            assert _latest_complete(rolled)["week_start"] == _THIS_MONDAY, day


def test_the_shim_changes_only_what_today_answers():
    """★ A harness that alters the code under test is a second defect.

    Same inputs through the pinned and the live namespace must give the same
    answers, and a plain date must come back as the SAME OBJECT — that is the
    isinstance branch, and proof the injected module did not push production's
    date down _as_date's string fallback.
    """
    live_as_date = _LIVE["_as_date"]
    d = _dt.date(2026, 8, 10)
    assert _as_date(d) is d and live_as_date(d) is d
    for v in (d, _dt.datetime(2026, 8, 10, 4, 5), "2026-08-10", None, "garbage", 12345):
        assert _as_date(v) == live_as_date(v), v
    assert _NS["_dt"].date.today() == _ASOF
    assert _LIVE["_dt"] is _dt, "the margin tests below are not on the real clock"


# ── the real clock, with a margin ────────────────────────────────────────────
# The pinned tests own the adjacent-week boundary. These keep the SHIPPED clock
# read in the suite — a pin proves the rule, it does not prove the function
# still reads a clock at all — and they are built with weeks of margin, so a
# midnight underneath them moves nothing: no rollover makes a four-week-old
# week current, or a fortnight-away week complete.

_live_latest_complete = _LIVE["_latest_complete"]


def test_the_live_clock_publishes_the_newest_complete_week():
    today = _dt.date.today()
    rolled = [_row(today - _dt.timedelta(days=28), 5),
              _row(today - _dt.timedelta(days=56), 9)]
    assert _live_latest_complete(rolled)["distinct_external_ips"] == 5, (
        "the NEWEST complete week must win, not simply the oldest row")


def test_the_live_clock_still_skips_a_week_that_is_not_over():
    """A week two weeks out is in progress under any clock this can run on."""
    today = _dt.date.today()
    rolled = [_row(today + _dt.timedelta(days=14), 999),
              _row(today - _dt.timedelta(days=28), 5)]
    assert _live_latest_complete(rolled)["distinct_external_ips"] == 5


# ── the supersession marker ──────────────────────────────────────────────────

def test_the_pre_correction_week_is_flagged():
    """★ The live case: 2026-08-10 predates #202 (2026-08-18 06:31Z)."""
    out = {}
    _mark_superseded(out, _dt.date(2026, 8, 10))
    assert out["superseded_by_correction"] is True
    assert out["superseded_by"][0]["ref"] == "dchub-mcp-server#202"
    assert "real_agents_7d" in out["superseded_note"], (
        "a flag that does not name the honest alternative is a dead end")


def test_a_post_correction_week_is_not_flagged():
    """★ THE FALSE BRANCH. A marker that fires on every week is not a marker."""
    out = {}
    _mark_superseded(out, _dt.date(2026, 9, 7))
    assert out == {}, "no keys at all on a clean week"


def test_marker_is_fail_soft():
    """Metadata about honesty must never take the endpoint down."""
    for bad in (None, "", "not-a-date", 12345):
        out = {}
        _mark_superseded(out, bad)
        assert out == {}


def test_marker_reuses_the_one_correction_registry():
    """★ NO TWIN. These timestamps must have exactly one home.

    A second copy of the correction dates in ai_reach.py is the drift class
    this codebase keeps paying for, so the guard asserts the import rather
    than the values.
    """
    src = _SRC[_SRC.index("def _mark_superseded"):_SRC.index("def _attach_canonical_7d")]
    assert "from routes.weekly_series import _superseded_by" in src
    assert "2026-08-18" not in src, "restated a correction timestamp locally"


def test_as_date_normalises_what_psycopg2_returns():
    assert _as_date(_dt.date(2026, 8, 10)) == _dt.date(2026, 8, 10)
    assert _as_date(_dt.datetime(2026, 8, 10, 4, 5)) == _dt.date(2026, 8, 10)
    assert _as_date("2026-08-10") == _dt.date(2026, 8, 10)
    assert _as_date(None) is None
