"""r-harvester-baseline (2026-09-02) — a week that is mostly a bulk scraper
must not serve as a WoW baseline, even when its TOTAL looks perfectly normal.

The defect these guards pin is specific and was live: 2026-W35 published
calls=1,810 against a trailing median of 2,447 (ratio 0.74, inside the
0.5-2.0 outlier band, so `wow_baseline_check` called it "in line") while
1,475 of those calls — 81.5% — were the named harvester `chain-hire`. The
outlier test compares TOTALS and structurally cannot see that.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_calls_deloop import CONCENTRATION_PCT  # noqa: E402
from routes.weekly_series import (  # noqa: E402
    _baseline_outlier_flag,
    _comparability,
    _harvester_dominated,
    _verdict,
    _wow,
)


def _week(start, calls, agents, harvester_calls=0, harvester_pct=None):
    pct = (harvester_pct if harvester_pct is not None
           else (round(100.0 * harvester_calls / calls, 1) if calls else None))
    return {
        "week_start": start, "calls": calls, "agents": agents,
        "partial": False, "status": "measured",
        "harvester_calls": harvester_calls,
        "harvester_pct": pct,
        "calls_net_of_harvesters": calls - harvester_calls,
        "harvester_names": ["chain-hire", "datacolo"],
    }


# ── the real-world shape, reproduced ────────────────────────────────────────
W35 = _week("2026-08-24", 1810, 35, harvester_calls=1475)   # 81.5% harvester
W36 = _week("2026-08-31", 340, 30, harvester_calls=0)


def test_the_live_w35_shape_is_flagged():
    hits = _harvester_dominated(["2026-08-24"], [W35])
    assert len(hits) == 1
    assert hits[0]["harvester_pct"] == 81.5
    assert hits[0]["calls_net_of_harvesters"] == 335
    assert hits[0]["threshold_pct"] == CONCENTRATION_PCT


def test_the_outlier_test_does_NOT_catch_it():
    """★ The whole reason this gate exists. If the ratio test ever did catch
    this shape, the new hazard would be redundant — so pin that it does not.
    1,810 against a median of 2,447 is 0.74: in line, by design."""
    weeks = [_week("2026-07-27", 8311, 84), _week("2026-08-03", 2367, 38),
             _week("2026-08-10", 2100, 72), _week("2026-08-17", 2527, 17),
             W35, W36]
    flag = _baseline_outlier_flag(weeks)
    assert flag["checked"] is True
    assert flag["baseline_week_start"] == "2026-08-24"
    assert flag["is_outlier"] is False, (
        "the ratio test now catches the harvester week — re-justify this gate")


def test_wow_against_that_baseline_is_refused():
    out = _wow([W35, W36])
    comp = out["comparability"]
    assert comp["includes_harvester_dominated_week"] is True
    assert comp["quotable_as_trend"] is False
    assert [h["week_start"] for h in comp["harvester_dominated_weeks"]] == ["2026-08-24"]
    # the arithmetic is still published — refused, not hidden
    assert out["calls_pct"] == round((340 - 1810) * 100.0 / 1810, 1)


def test_the_harvester_hazard_ALONE_flips_quotability():
    """★ ISOLATION TEST, and the one that matters.

    The W35/W36 pair above also crosses a real definition change (#294/#302,
    2026-09-01, inside W36), so its `quotable_as_trend is False` proves
    nothing about THIS hazard — a first pass of these guards asserted exactly
    that and survived deleting `harv` from the quotability conjunction. This
    pair postdates every registered change, so the harvester share is the only
    thing that can refuse it.
    """
    dirty = _week("2026-09-07", 1800, 35, harvester_calls=1470)   # 81.7%
    clean = _week("2026-09-14", 340, 30, harvester_calls=0)
    comp = _wow([dirty, clean])["comparability"]
    assert comp["crosses_definition_change"] is False, "pair must be otherwise clean"
    assert comp["superseded_by_correction"] is False, "pair must be otherwise clean"
    assert comp["includes_harvester_dominated_week"] is True
    assert comp["quotable_as_trend"] is False
    assert "NAMED BULK HARVESTER" in comp["means"]


def test_a_clean_pair_stays_quotable():
    """The gate must not refuse everything: two harvester-free weeks are a
    trend and still say so."""
    # ★ Weeks chosen to clear the OTHER two hazards, which is harder than it
    # looks: the real _DEFINITION_CHANGES entries land 2026-08-18 and
    # 2026-09-01, and EVERY week before the later of those is
    # superseded_by_correction — so no pair of past weeks is quotable at all
    # today. A quotable pair has to postdate both corrections.
    a = _week("2026-09-07", 2100, 72)
    b = _week("2026-09-14", 2200, 70)
    comp = _wow([a, b])["comparability"]
    assert comp["crosses_definition_change"] is False
    assert comp["superseded_by_correction"] is False
    assert comp["includes_harvester_dominated_week"] is False
    assert comp["quotable_as_trend"] is True


def test_share_below_threshold_does_not_trip():
    low = _week("2026-08-17", 2000, 40, harvester_calls=100)  # 5.0%
    assert _harvester_dominated(["2026-08-17"], [low]) == []


def test_share_exactly_at_threshold_trips():
    at = _week("2026-08-17", 1000, 40, harvester_pct=CONCENTRATION_PCT)
    assert len(_harvester_dominated(["2026-08-17"], [at])) == 1


def test_unmeasured_share_is_unknown_not_zero():
    """A week whose harvester query did not run must neither trip the gate nor
    be certified clean by it."""
    unknown = _week("2026-08-17", 2000, 40)
    unknown["harvester_pct"] = None
    assert _harvester_dominated(["2026-08-17"], [unknown]) == []
    assert _harvester_dominated(["2026-08-17"], None) == []


def test_only_weeks_inside_the_delta_are_considered():
    """A harvester week elsewhere in the 26-week series must not refuse a
    delta that never divided by it."""
    assert _harvester_dominated(["2026-08-31"], [W35, W36]) == []


def test_definition_change_still_wins_the_message():
    """Precedence: the three hazards are published independently, but `means`
    names the most serious one so a reader is not told the smaller story."""
    v = _verdict([{"effective_at": "x", "change": "c"}], [],
                 [{"week_start": "2026-08-24", "harvester_pct": 81.5}])
    assert v["crosses_definition_change"] is True
    assert v["includes_harvester_dominated_week"] is True
    assert v["quotable_as_trend"] is False
    assert "DIFFERENT population" in v["means"]


def test_verdict_stays_backward_compatible_for_span_callers():
    """comparability_for_spans calls _verdict with two args; that must keep
    working and must not invent a hazard."""
    v = _verdict([], [])
    assert v["includes_harvester_dominated_week"] is False
    assert v["harvester_dominated_weeks"] == []
    assert v["quotable_as_trend"] is True


def test_comparability_without_weeks_is_silent():
    """Every existing caller passes no week rows — none of them may start
    refusing deltas because of a hazard that cannot be evaluated."""
    v = _comparability(["2026-08-10", "2026-08-17"])
    assert v["includes_harvester_dominated_week"] is False


# ── the live partial week (2026-09-02 follow-up) ────────────────────────────
import datetime as _dt  # noqa: E402

from routes.weekly_series import _partial_week  # noqa: E402

_NOW = _dt.datetime(2026, 9, 2, 6, 0, tzinfo=_dt.timezone.utc)
_WK = _dt.date(2026, 8, 31)


def test_partial_week_reports_its_harvester_share():
    p = _partial_week(_WK, 30, 400, _NOW, harvester=(340, 60, 28))
    assert p["harvester_calls"] == 340
    assert p["harvester_pct"] == 85.0
    assert p["calls_net_of_harvesters"] == 60
    assert p["agents_net_of_harvesters"] == 28
    assert p["harvester_dominated"] is True
    assert "not demand" in p["harvester_warning"]


def test_partial_week_below_threshold_is_not_flagged():
    p = _partial_week(_WK, 30, 400, _NOW, harvester=(20, 380, 29))
    assert p["harvester_pct"] == 5.0
    assert p["harvester_dominated"] is False
    assert "harvester_warning" not in p


def test_partial_week_unmeasured_is_None_not_zero():
    """★ The shape #3581 actually shipped: no harvester keys at all. They must
    now be PRESENT and null when the companion query fails — absent keys and
    zero both read as 'no harvester here', which is the wrong answer."""
    p = _partial_week(_WK, 30, 400, _NOW)
    for k in ("harvester_calls", "harvester_pct", "calls_net_of_harvesters",
              "agents_net_of_harvesters", "harvester_names",
              "harvester_dominated"):
        assert k in p, f"{k} missing — a reader cannot tell unmeasured from clean"
        assert p[k] is None, k
    assert "harvester_warning" not in p


def test_partial_week_zero_calls_does_not_divide():
    p = _partial_week(_WK, 0, 0, _NOW, harvester=(0, 0, 0))
    assert p["harvester_pct"] is None
    assert p["harvester_dominated"] is False


def test_the_old_misleading_key_is_gone():
    """`baseline_harvester_dominated` described a narrower check than the code
    performs — it fires on the CURRENT week too. Renamed with no alias."""
    v = _verdict([], [], [{"week_start": "2026-08-24", "harvester_pct": 81.4}])
    assert "baseline_harvester_dominated" not in v
    assert "baseline_harvester" not in v
    assert v["includes_harvester_dominated_week"] is True
    assert len(v["harvester_dominated_weeks"]) == 1
