"""The third media loop runs on a clock, not on someone remembering it.

★★★ #3178 shipped the published-post reviewer WIRED AND VERIFIED LIVE — and
with no cron. It graded 4 posts, the critiques reached the composer prompt,
and then it only ever ran again if a human POSTed
/api/v1/media/published-review/run. One pass had run. A feedback loop that
depends on a person firing it is not a loop; it is a script.

★ IT CANNOT BE DRIVEN FROM THE EDGE. The pass makes an LLM call and
  Cloudflare's ROUTE_TIMEOUTS gives up at 15s — the hand-run returned
  {"error": "Service temporarily unavailable"} while the ORIGIN completed and
  wrote all four rows. A 503 there is not a failed job, and a scheduler that
  retried on it would double-grade. Off-request is the only correct host, so
  this lane lives in the worker's own scheduler thread and calls the pass
  in-process.

WHAT THIS MODULE GUARDS
  * the lane is on the clock AND in the dispatch registry (a SCHEDULE name
    missing from _RUNNERS is a SILENT no-op — the 2026-07-21 class);
  * the slot pair is single-hour, because CRAWLER_SCHEDULE=once is set in
    production and hour2 never fires;
  * the lane never reaches its own endpoint over HTTP;
  * it fails open — the caller is a cron tick with no handler of its own.

crawler_scheduler is imported directly here, as test_media_expansion_stories
does for the same contract. tests/test_scheduler_wiring.py keeps the separate
AST-only pass that checks EVERY entry rather than this one.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import crawler_scheduler as cs                                    # noqa: E402
import routes.media_published_review as pr                        # noqa: E402

LANE = "media_published_review"


def test_the_lane_is_both_scheduled_and_registered():
    """A SCHEDULE name missing from _RUNNERS silently no-ops: the dispatcher
    guards `name in _RUNNERS` with no exception, no log line and no metric.
    Assert BOTH halves — a tuple alone only makes the lane LOOK armed."""
    assert LANE in [s[2] for s in cs.SCHEDULE], "SCHEDULE tuple missing"
    assert LANE in cs._RUNNERS, (
        "_RUNNERS missing — the dispatcher would skip this lane forever while "
        "the SCHEDULE entry made it look armed")
    assert callable(cs._RUNNERS[LANE])
    entry = [s for s in cs.SCHEDULE if s[2] == LANE][0]
    assert entry[3] == "_run_media_published_review"


def test_the_slot_pair_is_single_hour_because_prod_fires_hour_one_only():
    """★★★ CRAWLER_SCHEDULE=once IS SET on dchub-worker (read off the live
    Railway env 2026-08-25), and _should_run_now does

        target_hours = [hour1] if once_a_day else [hour1, hour2]

    so hour2 NEVER FIRES in production. Two consequences; this test exists for
    the second, which is the one that bites silently:

      1. a (22, 10) pair would run once daily, not twice — merely misleading;
      2. _schedule_cadence_hours derives the DEAD-MAN cadence from the gap
         between the pair, so (22, 10) would claim 18h for a lane that in fact
         runs every 24h — and the public board would report it OVERDUE every
         single day, training the operator to ignore it.

    A single-hour pair is the honest declaration of a once-daily lane."""
    h1, h2 = [s[:2] for s in cs.SCHEDULE if s[2] == LANE][0]
    assert h1 == h2, (
        f"({h1}, {h2}) declares two slots but CRAWLER_SCHEDULE=once fires only "
        f"hour {h1}; the dead-man cadence would be computed from a gap that "
        "never happens and the lane would read OVERDUE daily")
    assert 0 <= h1 <= 23
    assert cs._SCHEDULE_CADENCE_H[LANE] >= 24, (
        "a once-daily lane needs more than 24h of dead-man grace or one late "
        f"slot reads as a dead lane (got {cs._SCHEDULE_CADENCE_H[LANE]}h)")


def test_the_lane_does_not_reach_its_own_endpoint_over_http():
    """★ The whole reason this is a worker lane. A loopback POST would put an
    LLM call behind the 15s edge timeout AND need an admin key it does not
    have (the 2026-07-06 self-request outage). Call the function."""
    import inspect
    src = inspect.getsource(cs._RUNNERS[LANE])
    assert "review_published_posts" in src, "the runner does not call the pass"
    for banned in ("requests.post", "_rq.post", "published-review/run",
                   "DCHUB_INTERNAL_API", "X-Admin-Key"):
        assert banned not in src, (
            f"{banned!r} — the lane went back to driving itself over HTTP, "
            "which is exactly what the 15s edge timeout kills")


def test_the_lane_never_raises_into_the_scheduler(monkeypatch):
    """★ FAIL-OPEN. The caller is a cron tick with no handler of its own, so
    an exception here aborts the remainder of the 22:00 slot for every OTHER
    lane too. review_published_posts already returns ok=False rather than
    raising; this guards the runner around it."""
    def boom(*a, **k):
        raise RuntimeError("model gateway is down")
    monkeypatch.setattr(pr, "review_published_posts", boom)
    cs._RUNNERS[LANE]()          # must not raise


def test_the_kill_switch_stops_the_lane(monkeypatch):
    monkeypatch.setenv("MEDIA_PUBLISHED_REVIEW_DISABLE", "1")
    called = []
    monkeypatch.setattr(pr, "review_published_posts",
                        lambda *a, **k: called.append(1) or {})
    cs._RUNNERS[LANE]()
    assert not called, "the kill switch did not stop the pass"


def test_the_lane_runs_the_pass_when_armed(monkeypatch):
    """★ POSITIVE CONTROL for the kill switch — a runner that never called
    anything at all would satisfy the test above just as well."""
    monkeypatch.delenv("MEDIA_PUBLISHED_REVIEW_DISABLE", raising=False)
    called = []
    monkeypatch.setattr(pr, "review_published_posts",
                        lambda *a, **k: called.append(1) or {"candidates": 0})
    monkeypatch.setattr(cs, "_stamp_cron_run", lambda *a, **k: None)
    cs._RUNNERS[LANE]()
    assert called, "the lane is armed but never ran the pass"


def test_the_pass_is_called_with_the_modules_own_defaults(monkeypatch):
    """★ The cron and the manual endpoint must grade the SAME window. Pinning
    days/limit in the runner would let the two drift apart silently, and the
    drift would only ever show up as a puzzling difference in candidates."""
    seen = {}
    monkeypatch.delenv("MEDIA_PUBLISHED_REVIEW_DISABLE", raising=False)
    monkeypatch.setattr(pr, "review_published_posts",
                        lambda *a, **k: seen.update(args=a, kw=k) or {})
    monkeypatch.setattr(cs, "_stamp_cron_run", lambda *a, **k: None)
    cs._RUNNERS[LANE]()
    assert seen.get("args") == () and seen.get("kw") == {}, (
        f"the lane pinned its own window {seen} instead of inheriting "
        "_DEFAULT_DAYS/_DEFAULT_LIMIT")


def test_the_slot_lands_after_the_days_publishing():
    """★ ORDERING, not just presence. The pass grades what SHIPPED, so it must
    run after the desk has published. Measured 2026-08-25 the three platforms
    last published at 15:58 / 20:13 / 20:30 UTC, and the composer's own slots
    run from 08:00 — an hour before that window would grade a stale day and
    steer nothing."""
    h1 = [s[0] for s in cs.SCHEDULE if s[2] == LANE][0]
    assert 21 <= h1 <= 23, (
        f"hour {h1} runs before the day's publishing has finished; the pass "
        "would grade yesterday and the critiques would miss today's posts")
