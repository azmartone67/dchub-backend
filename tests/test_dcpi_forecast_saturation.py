"""SH52-138: the DCPI forecast must not imply a verdict from a saturated projection.

THE DEFECT (measured live 2026-08-07, /api/v1/dcpi/scores/allen)
────────────────────────────────────────────────────────────────
The 12mo/24mo forecast projected BOTH excess_power_score:100 AND
constraint_score:100 with implied_verdict:AVOID for a market currently
65.8/59.2. Two scores simultaneously pinned to their ceiling is a clamp
artifact of the linear extrapolation (routes.dcpi._project caps to [0,100]),
not a plausible trajectory — and "excess_power 100 + AVOID" is
self-contradictory. It was served to anon crawlers on the free single-market
endpoint (a GEO/citation surface).

THE FIX
───────
When the raw linear projection runs past the [0,100] score bound before the
clamp, the horizon is flagged projection_saturated=True and NO implied_verdict
(and no verdict_change_from_now) is derived from it.

ANTI-VACUOUS
────────────
A gently-sloped market (does not saturate at 90 days) must STILL produce a
non-null implied_verdict at that horizon — otherwise this guard would pass by
nulling every forecast, which is not the behavior we want.
"""
import datetime as _dt

from routes.dcpi import _compute_forecast


def _history(excess_now, excess_slope, constraint_now, constraint_slope,
             n=30):
    """Build n daily samples ending today so that ys[-1] == *_now and the
    least-squares slope == *_slope (1-day spacing)."""
    now = _dt.datetime.now(_dt.timezone.utc)
    hist = []
    for i in range(n):
        # oldest first; newest (i == n-1) is the most recent observed value
        days_ago = (n - 1 - i)
        hist.append({
            "computed_at": now - _dt.timedelta(days=days_ago),
            "excess_power_score": excess_now - excess_slope * days_ago,
            "constraint_score": constraint_now - constraint_slope * days_ago,
            "time_to_power_months": 24.0,
        })
    return hist


def test_saturated_horizon_emits_no_implied_verdict():
    # now=70/68, slope 0.2/day: 90d stays < 100, 365d/730d blow past 100.
    hist = _history(70.0, 0.2, 68.0, 0.2)
    fc = _compute_forecast(hist, {"verdict": "BUILD"})
    assert fc.get("available") is True, fc
    proj = fc["projection"]

    for label in ("12mo", "24mo"):
        h = proj[label]
        assert h["projection_saturated"] is True, (label, h)
        # the self-contradictory "excess 100 + AVOID" must be gone
        assert h["implied_verdict"] is None, (label, h)
        assert h["verdict_change_from_now"] is None, (label, h)


def test_unsaturated_horizon_still_derives_a_verdict():
    # Same market: the 3mo (90-day) horizon does NOT saturate, so the guard
    # must NOT null it — proving the suppression is bound-triggered, not blanket.
    hist = _history(70.0, 0.2, 68.0, 0.2)
    fc = _compute_forecast(hist, {"verdict": "BUILD"})
    h = fc["projection"]["3mo"]
    assert h["projection_saturated"] is False, h
    assert h["implied_verdict"] is not None, h


def test_flat_market_never_saturates_and_keeps_its_verdict():
    # A flat (slope 0) market can never blow past the bound at any horizon.
    hist = _history(60.0, 0.0, 45.0, 0.0)
    fc = _compute_forecast(hist, {"verdict": "BUILD"})
    for label in ("3mo", "6mo", "12mo", "24mo"):
        h = fc["projection"][label]
        assert h["projection_saturated"] is False, (label, h)
        assert h["implied_verdict"] is not None, (label, h)
