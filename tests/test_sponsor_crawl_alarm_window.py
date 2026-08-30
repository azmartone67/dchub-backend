"""A missed sponsor-crawl day must be reported the same day, not three days on.

Every other daily job in the dead-man registry sits at cadence 36 (overdue at
2x = 72h) and that is correct for them: a daily job that misses a slot simply
runs again tomorrow, so a slow alarm costs nothing.

This job is different, and the difference is not a preference. Cloudflare
retains 8 days of request-level analytics for the zone — a 14-day query is
refused outright — so the per-engine crawl table an advertiser is invoiced
against can only be accumulated forward, one day at a time. A day missed is a
day that can never appear in any report again. At 72h the alarm arrives about
data that is already unrecoverable.

Measured drift on these two crons is 10-19 minutes (04:17 -> 04:27/04:29,
06:17 -> 06:35), so peak normal age is ~24.4h. A 36h threshold leaves ~11.6h of
headroom while still reporting a single missed slot within ~12 hours.
"""

import importlib.util
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")

_spec = importlib.util.spec_from_file_location(
    "_dmwatch", os.path.join(ROOT, "tools", "deadman", "watch.py"))
watch = importlib.util.module_from_spec(_spec)
sys.modules["_dmwatch"] = watch
try:
    _spec.loader.exec_module(watch)
except SystemExit:      # the module guards on env when run as a script
    pass

SPONSOR = ("sponsor-crawl-snapshot-daily.yml", "sponsor-crawl-snapshot.yml")

# One missed daily slot leaves ~48h since the last success. The threshold must
# sit below that to fire, and above normal peak age (~24.4h) not to false-red.
MUST_ALARM_BY_H = 48.0
NORMAL_PEAK_AGE_H = 24.4


def test_both_sponsor_crawl_feeds_are_registered():
    """Absence is not a decision — this registry says so itself. A feed dropped
    from WORKFLOWS stops being watched silently."""
    for wf in SPONSOR:
        assert wf in watch.WORKFLOWS, (
            f"{wf} is not in the dead-man registry — nothing would report it "
            f"dead, and its data cannot be backfilled once missed")


def test_a_single_missed_day_alarms_rather_than_three():
    for wf in SPONSOR:
        overdue_h = 2.0 * watch.WORKFLOWS[wf]
        assert overdue_h < MUST_ALARM_BY_H, (
            f"{wf} goes overdue at {overdue_h}h. One missed daily slot is "
            f"~48h of silence, so this threshold cannot report it. At the "
            f"house default of 36 (overdue 72h) up to THREE unbackfillable "
            f"days are lost before anyone is told.")


def test_the_threshold_still_clears_ordinary_cron_drift():
    """Tight is not free. Below ~24.4h peak age these would false-red daily —
    the 2026-07-30 incident this watcher's own margin check exists to prevent."""
    for wf in SPONSOR:
        overdue_h = 2.0 * watch.WORKFLOWS[wf]
        assert overdue_h > NORMAL_PEAK_AGE_H, (
            f"{wf} goes overdue at {overdue_h}h, below the ~{NORMAL_PEAK_AGE_H}h "
            f"a healthy daily cron reaches just before its next run — it would "
            f"alarm every day while the job is perfectly fine")


def test_the_registry_as_a_whole_still_clears_the_watcher_margin():
    """Guard the guard: tightening one feed must not break the invariant the
    watcher enforces across all of them."""
    floor = watch.WATCH_INTERVAL_H * watch.WATCH_MARGIN
    bad = {wf: cad for wf, cad in watch.WORKFLOWS.items() if (2.0 * cad) < floor}
    assert not bad, (
        f"feed(s) now go overdue sooner than the watcher's own {floor}h floor "
        f"and will false-red on ordinary drift: {bad}")


def test_the_registry_points_at_workflows_that_exist():
    """A cadence on a filename that is gone watches nothing."""
    for wf in SPONSOR:
        path = os.path.join(ROOT, ".github", "workflows", wf)
        assert os.path.exists(path), (
            f"{wf} is registered in the dead-man but the workflow file does "
            f"not exist — the feed can never beat and would sit permanently red")
