"""tests/test_media_pulse_rollup_verdict.py — /api/v1/media/pulse renamed every
'degraded' feed 'silent' (2026-08-17).

`severity_rank` mapped BOTH 'silent' and 'degraded' to 3, and the rollup then
recovered the winning NAME by reverse-lookup:

    worst = max(rank of each component verdict)          # -> 3
    verdict = next(v for v, rank in severity_rank.items() if rank == worst)

`next()` over a dict returns the first key at that rank — 'silent'. So any
degraded component published `"verdict": "silent"`.

Measured live 2026-08-17T23:49Z, the reading that surfaced this:
    components.press.verdict    = "healthy"   (12 press releases in 7d)
    components.linkedin.verdict = "degraded"  (18 posts in 7d, 3 stranded claims)
    components.winback.verdict  = "healthy"
    -> top-level "verdict": "silent", "ok": false

'silent' is documented as "nothing published recently (or ever)". The feed had
published 18 times that week and 4 times that day. media_organism, brain_qa,
brain_ownership_loop, brain_self_perception and the morning briefing all consume
this field.

Run:  python3 -m pytest tests/test_media_pulse_rollup_verdict.py -v
"""
from __future__ import annotations

from routes.dchub_media_revival import rollup_verdict


def _components(**verdicts):
    return {name: {"verdict": v} for name, v in verdicts.items()}


# ── THE PIN ────────────────────────────────────────────────────────────────

def test_degraded_component_does_not_publish_silent():
    """THE PIN — the exact live 2026-08-17 component reading."""
    verdict, ok = rollup_verdict(
        _components(press="healthy", linkedin="degraded", winback="healthy"))
    assert verdict == "degraded", (
        "a publisher shipping 18 posts in 7d must never be published as "
        "'silent' (= nothing published recently or ever)")
    assert ok is False


def test_silent_still_reports_silent():
    """Inverse control: the fix must not cost us the real 'silent' signal."""
    verdict, ok = rollup_verdict(
        _components(press="healthy", linkedin="silent", winback="healthy"))
    assert verdict == "silent"
    assert ok is False


def test_silent_outranks_degraded_when_both_present():
    """Nothing-at-all is worse than shipping-but-impaired, and the tie that
    caused this bug is gone, so the order is now decided, not incidental."""
    verdict, _ = rollup_verdict(_components(a="degraded", b="silent"))
    assert verdict == "silent"
    # order of the components must not change the answer
    verdict, _ = rollup_verdict(_components(a="silent", b="degraded"))
    assert verdict == "silent"


# ── the bands ──────────────────────────────────────────────────────────────

def test_all_healthy_is_healthy_and_ok():
    """Control: without this, every assertion here could pass vacuously."""
    assert rollup_verdict(_components(a="healthy", b="healthy")) == ("healthy", True)


def test_weak_is_reported_and_not_ok():
    assert rollup_verdict(_components(a="healthy", b="weak")) == ("weak", False)


def test_quiet_is_reported_but_still_ok():
    """'quiet' is a state, not a fault — winback with nothing to target."""
    assert rollup_verdict(_components(a="healthy", b="quiet")) == ("quiet", True)


def test_worst_component_wins_not_the_last_one():
    verdict, ok = rollup_verdict(
        _components(a="silent", b="healthy", c="healthy", d="healthy"))
    assert (verdict, ok) == ("silent", False)


# ── shape robustness ───────────────────────────────────────────────────────

def test_unknown_verdict_never_outranks_a_real_failure():
    """An unrecognised string ranks 0 — it must not mask a silent component
    by winning the max, nor invent a failure of its own."""
    verdict, ok = rollup_verdict(_components(a="banana", b="silent"))
    assert verdict == "silent" and ok is False
    assert rollup_verdict(_components(a="banana")) == ("banana", True)


def test_components_without_a_verdict_are_skipped():
    """`components.error` is a bare string, and a component read can fail and
    leave a dict with no verdict key. Neither may crash the rollup."""
    comps = dict(_components(linkedin="degraded"))
    comps["error"] = "OperationalError: timeout"
    comps["press"] = {"count_7d": 0}
    verdict, ok = rollup_verdict(comps)
    assert (verdict, ok) == ("degraded", False)


def test_no_components_reads_healthy():
    """Empty is the documented default and must not raise."""
    assert rollup_verdict({}) == ("healthy", True)
    assert rollup_verdict(None) == ("healthy", True)


# ── the pulse could not see two weeks of blindness (2026-08-18) ─────────────
#
# Every component was supply-side, so live the endpoint answered
#   {"verdict":"weak","reasons":["19 posts in 7d against a 21/wk floor"]}
# while the last successful engagement fetch was 2026-08-05 15:27Z and 53 posts
# carried NULL impressions/likes/comments. The publisher was fine; the FEEDBACK
# LOOP was dead, and the surface whose job is media health had no field for it.

from routes.dchub_media_revival import engagement_readback_verdict  # noqa: E402


def test_the_live_2026_08_18_reading_is_degraded():
    # 13 days since the last fetch, 53 of 53 posts unmeasurable.
    got = engagement_readback_verdict(13 * 24.0, 53, 53)
    assert got["verdict"] == "degraded"
    assert got["unmeasured"] == 53


def test_a_measurement_failure_NEVER_reports_silent():
    # ★ 'silent' means "nothing published recently (or ever)", and
    #   rollup_verdict carries the worst component's verdict STRING up to the
    #   aggregate. A dead readback emitting 'silent' would tell every consumer
    #   the publisher had stopped — the same false outage the 08-17 rank
    #   collision manufactured, arriving through a different door.
    for age in (None, 49.0, 24_000.0):
        for unmeasured, measurable in ((1, 1), (53, 53), (999, 1000)):
            v = engagement_readback_verdict(age, unmeasured, measurable)["verdict"]
            assert v != "silent", (age, unmeasured, measurable)


def test_nothing_to_measure_is_quiet_not_an_outage():
    # No posts means no readback is OWED. An empty window must not read as an
    # outage — nor as proof of health.
    assert engagement_readback_verdict(None, 0, 0)["verdict"] == "quiet"
    assert engagement_readback_verdict(99_999.0, 0, 0)["verdict"] == "quiet"


def test_a_fresh_readback_is_healthy_even_with_posts_still_pending():
    # Inside the tolerance the loop is working; recently-published posts simply
    # have not been scored yet.
    assert engagement_readback_verdict(3.0, 2, 20)["verdict"] == "healthy"


def test_never_fetched_with_posts_outstanding_is_degraded():
    assert engagement_readback_verdict(None, 4, 4)["verdict"] == "degraded"


def test_stale_but_everything_already_measured_is_healthy():
    # Staleness alone is not the failure — being unable to SCORE what we
    # published is. If nothing is outstanding there is nothing to report.
    assert engagement_readback_verdict(30 * 24.0, 0, 40)["verdict"] == "healthy"


def test_a_degraded_readback_degrades_the_whole_pulse():
    verdict, ok = rollup_verdict({
        "press":       {"verdict": "healthy"},
        "linkedin":    {"verdict": "healthy"},
        "measurement": engagement_readback_verdict(13 * 24.0, 53, 53),
    })
    assert (verdict, ok) == ("degraded", False), (
        "a publisher shipping into the dark must not roll up as healthy")


def test_an_UNMEASURED_readback_neither_certifies_nor_alarms():
    # The except branch emits no `verdict`, so rollup skips it: an unreadable
    # table is not evidence of health and must not manufacture an outage.
    verdict, ok = rollup_verdict({
        "press":       {"verdict": "healthy"},
        "measurement": {"measured": False, "error": "boom"},
    })
    assert (verdict, ok) == ("healthy", True)
