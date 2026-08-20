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
import datetime as _dt

_SRC = open("routes/ai_reach.py", encoding="utf-8").read()
_NS = {"_dt": _dt}
exec(_SRC[_SRC.index("def _as_date"):_SRC.index("def _attach_canonical_7d")], _NS)

_as_date = _NS["_as_date"]
_latest_complete = _NS["_latest_complete"]
_mark_superseded = _NS["_mark_superseded"]

# Anchored to the REAL today so the partial/complete split is whatever the
# function will actually see in production, not a frozen clock that stops
# testing the boundary the day it drifts.
_TODAY = _dt.date.today()
_THIS_MONDAY = _TODAY - _dt.timedelta(days=_TODAY.weekday())
_LAST_MONDAY = _THIS_MONDAY - _dt.timedelta(weeks=1)


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
