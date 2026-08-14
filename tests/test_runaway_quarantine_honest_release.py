"""Honest auto-release for runaway_finding quarantine rows (2026-08-14).

THE DEFECT THESE PIN DOWN: brain_findings_writer._maybe_quarantine_runaway
writes rows with fail_count = seen_count >= RUNAWAY_SEEN_THRESHOLD (200),
which equals brain_autopilot._QUARANTINE_RUNAWAY_FAIL (200) — so the item-16
runaway-immunize clause in _quarantined_patterns() kept EVERY row of the
class benched forever, while each row's last_reason promised "auto-release
in 24h". Nothing anywhere stamped released_at. Nine cron_silently_dead
platform self-alarms sat silenced 9-15 days on that 24h promise.

The fix has two halves, and each test here fails on the pre-fix source:
  1. autopilot_verify STAMPS released_at on runaway_finding: rows older
     than _RUNAWAY_RELEASE_HOURS (default 24, env BRAIN_RUNAWAY_RELEASE_HOURS).
  2. the writer's upsert re-arms a RELEASED row only on an exact NEW
     threshold crossing (seen == threshold), never on trailing sightings
     of the already-benched episode — so a release is not clobbered by
     the very next scan.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_ROOT = os.path.join(os.path.dirname(__file__), "..")


def _src(*parts) -> str:
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as fh:
        src = fh.read()
    assert len(src) > 1000, f"suspiciously empty source: {parts}"
    return src


# ── Half 1: the verify tick actually stamps released_at ──────────────

def test_verify_tick_stamps_released_at_for_runaway_rows():
    src = _src("routes", "brain_autopilot.py")
    m = re.search(
        r"UPDATE brain_pattern_quarantine\s+SET released_at = NOW\(\)",
        src)
    assert m, "no stamping UPDATE — the 24h promise is a lie again"
    block = src[m.start():m.start() + 700]
    assert "runaway_finding:%%" in block, (
        "stamp must be scoped to the runaway_finding: class — an unscoped "
        "stamp would also release item-16 verify-fail/no-op pattern benches")
    assert "released_at IS NULL" in block, "must only stamp unreleased rows"
    assert "make_interval(hours =>" in block, "window must be hour-bounded"


def test_release_window_knob_exists_and_is_env_tunable():
    src = _src("routes", "brain_autopilot.py")
    assert re.search(
        r'_RUNAWAY_RELEASE_HOURS\s*=\s*_env_int\("BRAIN_RUNAWAY_RELEASE_HOURS",\s*24\)',
        src), "window knob missing or default drifted from the 24h promise"


def test_stamp_lives_inside_the_verify_tick_not_dead_code():
    """The stamp must execute on the cron-driven verify pass — a helper
    nobody calls is the exact 'registered but never consumed' failure
    this whole class of bugs comes from."""
    src = _src("routes", "brain_autopilot.py")
    verify_start = src.index("def autopilot_verify()")
    # the next top-level statement (module-scope decorator or def) ends
    # the function body
    m = re.search(r"\n(@|def )", src[verify_start + 10:])
    assert m, "could not bound autopilot_verify body"
    body = src[verify_start:verify_start + 10 + m.start()]
    assert "SET released_at = NOW()" in body, (
        "stamping UPDATE is not inside autopilot_verify — it will never run")
    assert "runaway_released" in body, "summary must report the stamp count"


# ── Half 2: the writer re-arm cannot clobber a release ───────────────

class _Cur:
    def __init__(self):
        self.sqls, self.params = [], []
        self.rowcount = 1

    def execute(self, sql, args=None):
        self.sqls.append(sql)
        self.params.append(args)


def _call(seen, status="open"):
    from routes import brain_findings_writer as bfw
    cur = _Cur()
    bfw._maybe_quarantine_runaway(cur, "cron_silently_dead",
                                  "/api/jobs/gas-refresh", seen, status)
    inserts = [(s, p) for s, p in zip(cur.sqls, cur.params)
               if "INSERT INTO brain_pattern_quarantine" in s]
    return inserts


def test_upsert_rearm_is_gated_not_do_nothing():
    inserts = _call(200)
    assert inserts, "threshold crossing must register the quarantine row"
    sql, params = inserts[0]
    assert "DO NOTHING" not in sql, (
        "DO NOTHING means a released row can never re-bench — the flood "
        "guard silently dies after the first release")
    assert "released_at IS NOT NULL" in sql, (
        "re-arm must be gated on the row being RELEASED — an unconditional "
        "DO UPDATE refreshes quarantined_at on every sighting and the row "
        "never ages past the release window (permanent gag, again)")


def test_exact_crossing_sets_the_rearm_flag_true():
    sql, params = _call(200)[0]
    assert params[-1] is True, (
        "seen == threshold is the crossing sighting; it must be allowed "
        "to re-arm a released row")


def test_trailing_sighting_sets_the_rearm_flag_false():
    sql, params = _call(1015)[0]
    assert params[-1] is False, (
        "a trailing sighting of the SAME runaway episode re-armed the row — "
        "every release (owner or auto) would be clobbered within minutes")


def test_below_threshold_never_touches_the_table():
    assert _call(199) == [], "below threshold must be a no-op"


def test_resolved_finding_never_quarantined():
    assert _call(500, status="resolved") == [], (
        "resolved findings are exempt from the runaway guard")


def test_reason_text_no_longer_promises_what_it_cannot_do():
    src = _src("routes", "brain_findings_writer.py")
    assert "auto-release in 24h via brain_autopilot._quarantined_patterns" \
        not in src, (
            "the old reason text promised a release path that never fired; "
            "keep the text pointing at the stamped release")
    assert "BRAIN_RUNAWAY_RELEASE_HOURS" in src