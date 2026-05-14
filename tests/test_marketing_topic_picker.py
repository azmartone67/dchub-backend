"""Phase LL daily-press topic picker tests.

Guards the priority chain that ensures /api/v1/marketing/auto-generate
never goes a day without producing a press release. Previously the
endpoint skipped 29 of 30 days with "no_newsworthy_signal" — Phase LL
(PR #45) added _pick_daily_topic() with a fallback chain ending in
weekly_pulse (always returns something).

These tests verify each priority level + the always-on fallback.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ME = os.path.join(ROOT, "routes", "marketing_engine.py")


def _pick_daily_topic():
    """Extract _pick_daily_topic and exec it standalone."""
    src = open(ME, encoding="utf-8").read()
    m = re.search(
        r"^def _pick_daily_topic\(.*?(?=^def |^class |\Z)",
        src, re.DOTALL | re.MULTILINE,
    )
    assert m, "_pick_daily_topic not found in marketing_engine.py"
    ns = {}
    exec(m.group(0), ns)
    return ns["_pick_daily_topic"]


def test_priority_dcpi_mover_when_big_shift():
    """5pt+ DCPI shift wins over everything else."""
    pick = _pick_daily_topic()
    signals = {
        "biggest_movers": [{"market": "Atlanta", "delta": 7.5}],
        "top_build_markets": [{"market": "Phoenix", "excess": 60}],
    }
    topic, reason = pick(signals)
    assert topic == "dcpi_mover"
    assert "Atlanta" in reason


def test_dcpi_mover_skipped_when_shift_too_small():
    """A 2pt shift isn't newsworthy — should fall through to dcpi_leader."""
    pick = _pick_daily_topic()
    signals = {
        "biggest_movers": [{"market": "Atlanta", "delta": 2.0}],
        "top_build_markets": [{"market": "Phoenix", "excess": 60}],
    }
    topic, _ = pick(signals)
    assert topic == "dcpi_leader"


def test_dcpi_leader_when_no_movers():
    pick = _pick_daily_topic()
    signals = {"top_build_markets": [{"market": "Phoenix", "excess": 60}]}
    topic, reason = pick(signals)
    assert topic == "dcpi_leader"
    assert "Phoenix" in reason


def test_dcpi_warning_when_no_movers_no_builds():
    pick = _pick_daily_topic()
    signals = {"top_avoid_markets": [{"market": "Hayward", "constraint": 55}]}
    topic, reason = pick(signals)
    assert topic == "dcpi_warning"
    assert "Hayward" in reason


def test_new_facility_when_dcpi_quiet():
    pick = _pick_daily_topic()
    signals = {
        "new_facilities_24h": [{"name": "Acme DC", "provider": "Acme",
                                "mw": 75, "city": "Reno", "state": "NV"}],
    }
    topic, reason = pick(signals)
    assert topic == "new_facility"
    assert "Acme" in reason


def test_ai_adoption_when_traffic_present():
    pick = _pick_daily_topic()
    signals = {"ai_usage_24h": {"tool_calls": 5000, "unique_callers": 120}}
    topic, reason = pick(signals)
    assert topic == "ai_adoption"
    assert "5000" in reason or "5,000" in reason or "5_000" in reason or "callers" in reason


def test_always_falls_back_to_a_rotation_topic():
    """The single most important test: an empty signals dict still
    produces SOMETHING. This is what fixes the 'no_newsworthy_signal'
    skip path that bricked the daily heartbeat for 29 of 30 days.

    Phase LL+1 (2026-05-14): the final fallback changed from a single
    static 'weekly_pulse' topic to an 8-entry day-of-month rotation
    (auto_press was underproducing partly because the static fallback
    was so repetitive that Claude self-refused / validation rejected
    it as too similar to prior days). The contract is unchanged in
    spirit — empty signals ALWAYS yield a valid (topic, reason) tuple
    — only the specific topic set grew.
    """
    pick = _pick_daily_topic()
    topic, reason = pick({})
    rotation_topics = {
        "iso_grid_pulse", "water_risk_brief", "fiber_capacity_map",
        "interconnection_queue", "permit_velocity", "tax_incentive_brief",
        "ma_pulse", "methodology_explainer",
    }
    assert topic in rotation_topics, f"unexpected fallback topic: {topic}"
    assert reason  # non-empty
    # Determinism: same day → same topic on repeated calls
    topic2, _ = pick({})
    assert topic == topic2, "fallback must be deterministic within a day"


def test_priority_order_holds_with_all_signals_present():
    """When every signal is present, dcpi_mover (highest priority)
    wins — guards against any future refactor that flips the chain."""
    pick = _pick_daily_topic()
    signals = {
        "biggest_movers": [{"market": "Atlanta", "delta": 8.0}],
        "top_build_markets": [{"market": "Phoenix", "excess": 60}],
        "top_avoid_markets": [{"market": "Hayward", "constraint": 55}],
        "new_facilities_24h": [{"name": "Acme", "mw": 100}],
        "ai_usage_24h": {"tool_calls": 5000, "unique_callers": 120},
    }
    topic, _ = pick(signals)
    assert topic == "dcpi_mover"
