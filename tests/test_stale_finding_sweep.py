"""A sweep that trusts staleness is one failed scan from emptying the queue.

"Close what hasn't been seen lately" is correct only when you can prove the
scanner looked. If the nightly run fails, is misconfigured, or checks out the
wrong root, EVERY row goes stale in the same instant and an unfloored sweep
reads that as "everything got fixed". It would report a triumphant number,
destroy the queue, and the damage would be invisible — an empty queue looks
exactly like a healthy one.

So these tests are mostly about REFUSING. The floors are the feature; the
closing is the easy part.

evaluate_floors is pure on purpose: the whole safety argument is decidable
without a database, so it can be tested exhaustively instead of sampled.

Stdlib + pytest; no DB, no network.
"""
import pytest

from routes import brain_stale_finding_sweep as s

# A healthy night: scan wrote 2h ago, refreshed 48 rows, 4 of 48 went stale.
HEALTHY = dict(newest_age_h=2.0, refreshed_in_window=48,
               open_total=48, stale_total=4)


def _ev(**kw):
    return s.evaluate_floors(**{**HEALTHY, **kw})


def _floors(refusals):
    return {r["floor"] for r in refusals}


def test_a_healthy_night_is_allowed():
    allowed, refusals, cap = _ev()
    assert allowed is True, refusals
    assert refusals == []


# ── floor 1: the scanner must have run ──────────────────────────────────────
def test_a_dead_scanner_cannot_sweep_its_own_queue_to_zero():
    """The failure this exists for. Scan last wrote 5 days ago, so all 48 rows
    are stale -- which is a fact about the SCANNER, not about the findings."""
    allowed, refusals, _ = _ev(newest_age_h=120.0, refreshed_in_window=0,
                               stale_total=48)
    assert allowed is False
    assert "liveness" in _floors(refusals)


def test_a_detector_that_never_wrote_anything_is_refused():
    allowed, refusals, _ = _ev(newest_age_h=None, refreshed_in_window=0,
                               open_total=0, stale_total=0)
    assert allowed is False
    assert "liveness" in _floors(refusals)


def test_the_window_survives_one_missed_nightly_run():
    """05:13Z daily: a single miss is ~48h... but the floor is 36h, so one miss
    REFUSES. That is deliberate -- pin it so a later widening is a decision."""
    assert _ev(newest_age_h=35.9)[0] is True
    assert _ev(newest_age_h=36.1)[0] is False


# ── floor 2: the scanner must have found something ──────────────────────────
def test_a_scan_that_matched_almost_nothing_is_refused():
    """A stale glob or the wrong checkout root writes ONE row and passes
    liveness. Volume is what tells that apart from a real scan."""
    allowed, refusals, _ = _ev(refreshed_in_window=1)
    assert allowed is False
    assert "refresh_volume" in _floors(refusals)


def test_volume_floor_is_inclusive_at_its_limit():
    assert _ev(refreshed_in_window=s._MIN_REFRESHED)[0] is True
    assert _ev(refreshed_in_window=s._MIN_REFRESHED - 1)[0] is False


# ── floor 3: a shape change is one regression, not N fixes ──────────────────
def test_a_key_shape_change_is_refused_even_though_the_scan_is_healthy():
    """The subtle one. The scan ran, refreshed 48 rows -- but they are all NEW
    rows under a new URL format, so the 48 OLD rows went stale together. Both
    earlier floors pass. Only the proportion cap catches it."""
    allowed, refusals, cap = s.evaluate_floors(
        newest_age_h=1.0, refreshed_in_window=48,
        open_total=48, stale_total=48)
    assert allowed is False
    assert "proportion" in _floors(refusals)
    assert cap == 14   # max(3, int(48*0.30))


def test_a_small_queue_can_still_be_cleared():
    """A pure fraction would pin tiny queues shut forever: int(4*0.3)=1."""
    allowed, _, cap = s.evaluate_floors(newest_age_h=1.0, refreshed_in_window=10,
                                        open_total=4, stale_total=3)
    assert cap == s._MIN_ABS_SWEEP == 3
    assert allowed is True


# ── refusals must be complete, not first-wins ───────────────────────────────
def test_every_failing_floor_is_reported_not_just_the_first():
    """A scan can be both dead AND shape-changed. Reporting one hides the next
    until someone fixes the first and meets it as a surprise."""
    allowed, refusals, _ = s.evaluate_floors(
        newest_age_h=200.0, refreshed_in_window=0,
        open_total=48, stale_total=48)
    assert allowed is False
    assert _floors(refusals) == {"liveness", "refresh_volume", "proportion"}


@pytest.mark.parametrize("r", [
    dict(newest_age_h=200.0, refreshed_in_window=0, stale_total=48),
    dict(refreshed_in_window=1),
    dict(stale_total=48),
])
def test_every_refusal_names_a_floor_a_value_and_a_limit(r):
    _, refusals, _ = _ev(**r)
    assert refusals
    for x in refusals:
        assert x["floor"] and x["why"]
        assert "value" in x and "limit" in x


# ── the status choice is load-bearing ───────────────────────────────────────
def test_the_sweep_writes_resolved_and_not_a_status_only_some_readers_honour():
    """brain_findings consumers disagree on what counts as closed:
        NOT IN ('resolved','closed') / ('resolved','wont_fix')
        / ('resolved','wont_fix','dismissed')
    'dismissed' is the truer word and would leave the row OPEN to three of the
    seven. A finding closed on one surface and open on another is worse than an
    imprecise label."""
    src = open("routes/brain_stale_finding_sweep.py").read()
    assert "status = 'resolved'" in src
    assert "status = 'dismissed'" not in src
    assert "status = 'wont_fix'" not in src


def test_every_swept_row_carries_the_audit_marker():
    """'resolved' feeds findings_resolved_7d/30d, so a sweep inflates a metric
    the operator reads as fixes. The marker is what makes that subtractable."""
    src = open("routes/brain_stale_finding_sweep.py").read()
    assert 'MARKER = "[stale-sweep]"' in src
    assert "detail = COALESCE(detail,'') || %s" in src
    assert "WITHDRAWN by its detector, not fixed" in src


def test_the_update_re_checks_open_at_write_time():
    """The candidate list is read before the UPDATE; a concurrent resolve in
    between must not be overwritten."""
    src = open("routes/brain_stale_finding_sweep.py").read()
    i = src.index("UPDATE brain_findings")
    assert "COALESCE(status,'open') = 'open'" in src[i:i + 500]
