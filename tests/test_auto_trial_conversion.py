"""The auto-trial conversion detector that was promised and never built.

House rule: tests NEVER import main. Everything here imports leaf modules or
reads files directly, and nothing runs at module scope.

routes/auto_trial.py has said since May: "Brain detector
check_auto_trial_conversion_rate fires if <20% of trial keys -> real signups
within 7 days. Tracks the fix's impact." That name appeared exactly once in the
whole tree — in that docstring. The fix built against 7,839 paywall signals -> 6
conversions (0.08%) shipped three months ago and nothing ever measured it.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(*parts) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def test_the_promised_detector_now_exists_and_is_registered():
    """A detector that is not in the run list is a docstring with extra steps —
    which is exactly what the last three months looked like."""
    src = _src("routes", "brain_consistency_radar.py")
    assert "def check_auto_trial_conversion_rate()" in src
    # Registered in the detector run list, not merely defined.
    runlist = src[src.index("check_mcp_conversion_stale,"):]
    assert "check_auto_trial_conversion_rate," in runlist[:400]


def test_the_detector_measures_signup_not_usage():
    """★A USED TRIAL KEY IS NOT A CONVERSION. The real hop is signed_up_email —
    a human bound an address. call_count > 0 is an agent retrying with a free
    key it was handed."""
    src = _src("routes", "brain_consistency_radar.py")
    fn = src[src.index("def check_auto_trial_conversion_rate()"):]
    fn = fn[:fn.index("def check_mcp_conversion_stale")]
    assert "signed_up_email IS NOT NULL" in fn
    assert "upgraded_tier" in fn, "the paid hop should be reported too"


def test_only_keys_old_enough_to_have_had_their_week_are_scored():
    """Scoring keys minted this morning as failures would make the rate a
    function of how recently the cron ran."""
    src = _src("routes", "brain_consistency_radar.py")
    fn = src[src.index("def check_auto_trial_conversion_rate()"):]
    fn = fn[:fn.index("def check_mcp_conversion_stale")]
    assert "minted_at <  NOW() - INTERVAL '7 days'" in fn


def test_the_rate_is_declared_a_percent_not_a_tally():
    """It writes int(rate * 100) into `count`. Undeclared, brain_work_selector
    would read 18 as eighteen sightings — the #48 class, in a brand-new
    detector written by someone who knew about it."""
    src = _src("routes", "brain_consistency_radar.py")
    fn = src[src.index("def check_auto_trial_conversion_rate()"):]
    fn = fn[:fn.index("def check_mcp_conversion_stale")]
    assert '"count_kind": "percent"' in fn
    from routes.brain_work_selector import occurrence_signal
    occ, why = occurrence_signal(
        {"issue": "auto_trial_signup_rate_low", "count": 18,
         "count_kind": "percent"})
    assert occ == 0 and why["source"] == "declared_value"


def test_a_small_cohort_is_not_a_finding():
    """Below the sample floor the rate is noise. A detector that fires on
    three keys teaches the operator to ignore it."""
    src = _src("routes", "brain_consistency_radar.py")
    assert "_AUTO_TRIAL_MIN_SAMPLE" in src
    assert "_AUTO_TRIAL_SIGNUP_FLOOR" in src


def test_the_floor_is_the_one_auto_trial_named():
    from routes import brain_consistency_radar as r
    assert abs(r._AUTO_TRIAL_SIGNUP_FLOOR - 0.20) < 1e-9, (
        "auto_trial.py promised <20%; changing the floor silently rewrites "
        "what the May fix was measured against")


def test_free_trial_usage_is_named_where_it_is_counted_as_conversion():
    """★check_mcp_conversion_stale adds auto_trial_keys WHERE call_count > 0 to
    `conversions`. That is free usage, and folding it in unlabelled is what
    made the funnel read healthy while licence sales stayed flat. The
    threshold logic is deliberately unchanged — only the labelling."""
    src = _src("routes", "brain_consistency_radar.py")
    i = src.index("auto_trial_conv = int(")
    window = src[i:i + 700]
    assert "activation, not" in window and "revenue" in window


# ── the correction to loop-control lane 8 ─────────────────────────────

def test_lane_8_no_longer_blames_a_bug_that_was_already_fixed():
    """★The lane said the relay token is single-use and auto-redeemed in
    ~0.85s, returning 410 Gone. That describes the upgrade CLAIM, not the
    relay: routes/human_relay.py is stateless at mint and renders a useful
    page even for a bad token. Left uncorrected, the lane sends every reader
    at a bug that does not exist — I was the first one it sent."""
    src = _src("routes", "loop_control_master_shell.py")
    i = src.index('"human_opened", "a REAL human has opened a relay link",\n        real > 0,')
    window = src[i:i + 1400]
    assert "CORRECTED 2026-08-03" in window
    assert "STATELESS at mint" in window
    assert "check_relay_opens.py" in window


def test_the_relay_really_is_stateless_at_mint():
    """The claim the correction rests on, checked against the source rather
    than trusted."""
    src = _src("routes", "human_relay.py")
    assert "Stateless at mint" in src
    assert "never a dead end" in src
    body = "\n".join(l for l in src.splitlines()
                     if not l.lstrip().startswith("#"))
    assert "410" not in body, "the relay page returns no Gone status"
