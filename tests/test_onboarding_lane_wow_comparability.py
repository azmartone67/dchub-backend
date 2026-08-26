"""The onboarding lane called a definition change a demand collapse (2026-08-26).

`_sense_onboarding` queries mcp_calls_identity directly, so it never passed
through `_mark_wow_comparability` — the guard flask_mcp_endpoints added on
2026-08-20 for this exact window pair. Live 2026-08-26T21:28Z the funnel
published `real_external_agents_wow_pct: null` + `_withheld: -70.4` + a reason,
while this lane handed the model `agents_7d 16` and `agents_prior_7d 56` and it
did the division itself: "a -71% WoW collapse" on seven consecutive ticks.

#202 (2026-08-18 06:31Z) stopped counting our own GitHub Actions as external
demand (49.9% of is_real_external over 60d -> 0.05%). Current window clean,
prior window wholly pre-fix. The fixtures use the real 16/56 reading.
"""
import pytest
from routes import brain_lane_driver as bld
from routes import weekly_series

_NOT_QUOTABLE = {"quotable_as_trend": False,
                 "means": "at least one week in this delta counts a DIFFERENT "
                          "population from the others — see changes[]."}
_QUOTABLE = {"quotable_as_trend": True, "means": None}


def _sense(monkeypatch, comp, row=(16, 56, 1952)):
    monkeypatch.setattr(bld, "_q1", lambda *a, **k: row)
    if comp is not None:
        monkeypatch.setattr(weekly_series, "comparability_for_spans",
                            lambda spans: comp)
    return bld._sense_onboarding()


def test_prior_is_withheld_across_the_202_correction(monkeypatch):
    """THE REGRESSION: the model must not receive two divisible levels."""
    m = _sense(monkeypatch, _NOT_QUOTABLE)
    assert m["agents_prior_7d"] is None, (
        f"handed the model prior={m['agents_prior_7d']} beside agents_7d="
        f"{m['agents_7d']} — it will divide them and file a phantom collapse")
    assert m["agents_prior_7d_withheld"] == 56
    assert m["agents_prior_7d_withheld_reason"]


def test_the_level_and_kpi_survive_withholding(monkeypatch):
    """Levels are always safe; kpi_main is what the lane is graded on."""
    m = _sense(monkeypatch, _NOT_QUOTABLE)
    assert m["agents_7d"] == 16
    assert m["kpi_main"] == 16.0
    assert m["real_calls_7d"] == 1952


def test_prior_passes_through_when_windows_ARE_comparable(monkeypatch):
    """CHEAT guard: a version that always nulls the prior passes every test
    above. Once the contaminated window rolls off (~2026-09-01) the delta is
    honest again and must come back."""
    m = _sense(monkeypatch, _QUOTABLE)
    assert m["agents_prior_7d"] == 56
    assert "agents_prior_7d_withheld" not in m


def test_fail_soft_when_comparability_raises(monkeypatch):
    """Comparability is metadata about honesty — losing it must not cost the
    sense payload, which is what the lane actually runs on."""
    def boom(spans): raise RuntimeError("weekly_series down")
    monkeypatch.setattr(weekly_series, "comparability_for_spans", boom)
    m = _sense(monkeypatch, None)
    assert m["agents_7d"] == 16 and m["kpi_main"] == 16.0
    assert m["agents_prior_7d"] == 56


def test_db_error_path_untouched(monkeypatch):
    monkeypatch.setattr(bld, "_q1", lambda *a, **k: None)
    m = bld._sense_onboarding()
    assert m["error"] == "db" and m["kpi_main"] == 0.0
