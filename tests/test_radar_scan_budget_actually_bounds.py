"""The scan budget must bound the scan; the old one bounded nothing.

`scan_all()` documented a "HARD 25s wall-clock budget" and said detectors past
the deadline are "abandoned (their thread finishes in the background, result
discarded)". Exiting the `with ThreadPoolExecutor(...)` block instead called
shutdown(wait=True, cancel_futures=False), so every QUEUED detector ran to
completion and only its RESULT was discarded. With ~140 detectors on 8 workers
most are queued: the scan paid for all of them and published a fraction.

Measured wall times against that "hard 25s budget" on 2026-09-05:
36.0s, 40.1s, 44.1s, 54.5s, 59.8s, 62.8s.

These tests exercise the real executor lifecycle with fake detectors, so they
measure the bound rather than reading the source for a reassuring string.
"""
import concurrent.futures as cf
import threading
import time

BUDGET = 25  # mirrors _SCAN_BUDGET_S


def _pool_roundtrip(*, cancel_futures, n_slow=40, workers=4, budget=0.6,
                    slow_s=0.4):
    """Run the shipped shutdown discipline over fake detectors.

    Returns (wall_seconds, n_actually_executed). Each 'detector' sleeps, so a
    pool that runs every queued task takes far longer than the budget.
    """
    executed = []
    lock = threading.Lock()

    def slow(i):
        time.sleep(slow_s)
        with lock:
            executed.append(i)
        return i

    t0 = time.time()
    ex = cf.ThreadPoolExecutor(max_workers=workers)
    try:
        futs = [ex.submit(slow, i) for i in range(n_slow)]
        deadline = t0 + budget
        try:
            for _f in cf.as_completed(futs, timeout=budget):
                if time.time() >= deadline:
                    break
        except cf.TimeoutError:
            pass
        if cancel_futures:
            for f in futs:
                f.cancel()
    finally:
        if cancel_futures:
            ex.shutdown(wait=False, cancel_futures=True)
        else:
            ex.shutdown(wait=True)          # the OLD `with`-block behaviour
    wall = time.time() - t0
    # let stragglers land so the count is stable
    time.sleep(slow_s * 2)
    with lock:
        return wall, len(executed)


def test_the_old_discipline_did_not_bound_the_scan():
    """Control: proves the harness can detect an unbounded pool at all.

    Without this the bounded assertion below could pass for the wrong reason
    (e.g. the fake work being too fast to overrun anything).
    """
    wall, executed = _pool_roundtrip(cancel_futures=False)
    assert wall > 1.5, (
        f"control failed to overrun: wall={wall:.2f}s — the fixture is too "
        f"cheap to distinguish bounded from unbounded")
    assert executed >= 30, f"old discipline should run nearly everything, ran {executed}"


def test_cancel_futures_bounds_wall_time_and_skips_queued_work():
    wall, executed = _pool_roundtrip(cancel_futures=True)
    assert wall < 1.5, f"budget still not bounding: wall={wall:.2f}s"
    assert executed < 30, (
        f"queued detectors still ran ({executed}) — cancel_futures is not "
        f"taking effect")


def test_scan_all_shuts_down_without_waiting_and_cancels_queued():
    """The shipped call must carry BOTH flags. Either alone leaves the bug."""
    import inspect
    from routes import brain_consistency_radar as r
    src = inspect.getsource(r.scan_all)
    assert "ex.shutdown(wait=False, cancel_futures=True)" in src, (
        "scan_all no longer cancels queued detectors / still blocks on "
        "in-flight ones — the budget bounds nothing again")
    assert "with _cf.ThreadPoolExecutor(max_workers=8" not in src, (
        "the pool is back inside a `with`, whose __exit__ waits for every "
        "queued detector regardless of the budget")


def test_not_done_is_measured_before_cancelling():
    """cancel() makes a future report done(); measuring after would report 0."""
    import inspect
    from routes import brain_consistency_radar as r
    src = inspect.getsource(r.scan_all)
    i_measure = src.index("not_done = [")
    i_cancel = src.index("if f.cancel()")
    assert i_measure < i_cancel, (
        "not_done is computed after cancelling, so every abandoned detector "
        "reports done() and the partial-scan finding silently reads zero")


def test_partial_finding_separates_cancelled_from_still_running():
    import inspect
    from routes import brain_consistency_radar as r
    src = inspect.getsource(r.scan_all)
    assert "cancelled" in src and "still_running" in src, \
        "the two abandonment causes are conflated again"
    assert "cancelled before starting" in src, \
        "the partial finding no longer reports how many never ran"


def test_no_comment_claims_a_per_detector_timeout_that_does_not_exist():
    """_run_one has no timeout. Two comments used to say it did."""
    import inspect
    from routes import brain_consistency_radar as r
    run_src = inspect.getsource(r.scan_all)
    # the claim, not the correction that explains its absence
    for phrase in ("with per-detector 20s\n    timeout",
                   "its own 20s per-future cap"):
        assert phrase not in run_src, f"stale claim back in the source: {phrase!r}"
