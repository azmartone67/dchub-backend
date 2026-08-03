"""Guard: the cron heartbeat must not STACK a job that cannot overlap itself.

WHAT THIS PINS
──────────────
routes/cron_heartbeat.py fires every _DISPATCH entry whose predicate matches the
current UTC minute, on EVERY heartbeat. That is safe by design for the rest of
the table — the entries are DELETEs, upserts, or dedup-logged sends — and the
minute windows are deliberately WIDE because GitHub drops most of the scheduled
5-minute fires (measured 2026-08-02: exactly ONE fire landed in the whole 04:00
hour). The file states that contract outright: "every job here is idempotent, so
overlapping/repeat fires within a window are harmless."

★★ land_power_sync_incremental BREAKS IT. Its predicate is
`hour == 4 and minute < 55` — a 55-minute window — and it POSTs
/api/land-power/sync, which spawns an UNGUARDED daemon thread running a
4-source crawl of 75k+ row ArcGIS layers. hifld-substations alone measured
4,740s (79 minutes) on 2026-08-02. NEITHER land-power entry point has a
single-flight guard, so every re-fire inside that window starts ANOTHER full
crawl writing the same rows to the same tables. cron-heartbeat.yml is scheduled
'1-59/5 * * * *', so a healthy delivery day is up to ELEVEN concurrent writers
racing on substations_name_lat_lng_uniq — and that source currently reports
verdict=never_succeeded with 73,328 duplicate-key errors out of 75,328 fetched.

THE CONTRACT
────────────
  R1. A label with a declared re-fire window fires ONCE, then is suppressed for
      the rest of that window.
  R2. Suppression is per LABEL — one job being suppressed never withholds
      another job that is genuinely due.
  R3. The suppressed label is still REPORTED (in `skipped`), because a job that
      silently vanishes from both lists is indistinguishable from one that was
      never scheduled — the flattering-green this repo keeps paying for.
  R4. land_power_sync_incremental specifically carries a window. It is the entry
      that motivated this, and re-adding it to _DISPATCH without one must fail.
  R5. Entries with NO declared window keep firing every time — this must not
      quietly become a global rate limiter on the other ~100 jobs.

★ NO NETWORK, NO DB, NO FLASK APP. The suppression helper is pure, so this
tests it directly rather than booting the blueprint.
"""
import datetime
import importlib

import pytest

hb = importlib.import_module("routes.cron_heartbeat")


def _fresh():
    """Clear the module's in-process fire ledger between cases."""
    with hb._LAST_FIRED_LOCK:
        hb._LAST_FIRED.clear()


# ── R1 ────────────────────────────────────────────────────────────────────────
def test_a_windowed_label_fires_once_then_is_suppressed():
    _fresh()
    t0 = datetime.datetime(2026, 8, 2, 4, 1, 0)
    assert hb._refire_suppressed("land_power_sync_incremental", t0) is False, \
        "the first fire in a window must be allowed"
    for minute in (6, 11, 26, 54):
        t = t0.replace(minute=minute)
        assert hb._refire_suppressed("land_power_sync_incremental", t) is True, (
            f"04:{minute:02d} re-fired inside the window — that is a second "
            f"concurrent 4-source crawl on the same tables")


def test_the_window_expires():
    _fresh()
    t0 = datetime.datetime(2026, 8, 2, 4, 1, 0)
    assert hb._refire_suppressed("land_power_sync_incremental", t0) is False
    window = hb._MIN_REFIRE_S["land_power_sync_incremental"]
    later = t0 + datetime.timedelta(seconds=window + 1)
    assert hb._refire_suppressed("land_power_sync_incremental", later) is False, \
        "suppression must lapse, or the job never runs again in this process"


# ── R2 / R5 ───────────────────────────────────────────────────────────────────
def test_suppression_is_per_label_and_only_for_declared_windows():
    _fresh()
    t0 = datetime.datetime(2026, 8, 2, 4, 1, 0)
    hb._refire_suppressed("land_power_sync_incremental", t0)
    # An unrelated, undeclared label must be unaffected — and must keep firing
    # every single time, because the rest of the table RELIES on repeat fires
    # to survive GitHub dropping most heartbeats.
    for minute in (1, 6, 11):
        t = t0.replace(minute=minute)
        assert hb._refire_suppressed("industry_pulse_refresh", t) is False, \
            "an entry with no declared window was rate-limited"


# ── R4 ────────────────────────────────────────────────────────────────────────
def test_land_power_is_declared_non_reentrant():
    labels = {row[0] for row in hb._DISPATCH}
    if "land_power_sync_incremental" not in labels:
        pytest.skip("entry retired from _DISPATCH — nothing left to suppress")
    assert "land_power_sync_incremental" in hb._MIN_REFIRE_S, (
        "land_power_sync_incremental is back in _DISPATCH with NO re-fire "
        "window — its 55-minute predicate will stack concurrent 79-minute "
        "crawls on substations/transmission again")
    assert hb._MIN_REFIRE_S["land_power_sync_incremental"] >= 3600, (
        "the window is shorter than the crawl itself, so a re-fire still "
        "overlaps a run that is very much still going")


# ── R3 ────────────────────────────────────────────────────────────────────────
def test_a_suppressed_job_is_still_reported():
    """It must land in `skipped`, not vanish. Reading the heartbeat's own
    source is the cheap way to pin this without booting Flask."""
    import inspect
    src = inspect.getsource(hb.heartbeat)
    assert "_refire_suppressed" in src, \
        "the heartbeat does not consult the suppression ledger at all"
    assert "re-fire suppressed" in src, (
        "a suppressed job is dropped from BOTH due and skipped — it becomes "
        "invisible, which is how a dead job looks identical to a healthy one")


# ── must-fail control — never delete ─────────────────────────────────────────
@pytest.mark.xfail(strict=True, reason="must-fail control: proves this file runs")
def test_zzz_must_fail_control():
    assert False, "control"
