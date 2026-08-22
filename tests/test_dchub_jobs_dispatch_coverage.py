"""dchub-jobs.yml dispatch coverage (2026-08-02).

Two independent bugs let scheduled jobs die silently for weeks, and neither
touched a line of application code:

  1. WALL-CLOCK DISPATCH. The step read `date -u` and matched half-hour jobs on
     MINUTE in 30-39. GitHub routinely starts scheduled runs late under load, so
     a late run fell through to `keep-alive` and reported SUCCESS. Measured:
     the 12:30 evolution slot started 12:46 and dispatched nothing.
  2. CRON/PATTERN DRIFT. market-report's cron was changed to '52 14 * * *' while
     the dispatcher still matched "14:30"|"14:3"* — so it could never fire, not
     even on a punctual run.

Both are now structurally impossible: dispatch keys on `github.event.schedule`
(the cron GitHub matched), and these pins enforce a 1:1 cron<->arm mapping.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(ROOT, ".github", "workflows", "dchub-jobs.yml")


def _src() -> str:
    with open(WF, encoding="utf-8") as fh:
        return fh.read()


def _declared_crons(src: str) -> set:
    return set(re.findall(r"^\s+- cron:\s*'([^']+)'", src, re.M))


def _dispatch_arms(src: str) -> set:
    """Cron strings the TRIGGER_CRON case actually handles."""
    m = re.search(r'case "\$\{TRIGGER_CRON\}" in(.*?)\n\s+esac', src, re.S)
    assert m, "TRIGGER_CRON case block not found — dispatch was rewritten?"
    return set(re.findall(r'^\s*"([^"]+)"\)', m.group(1), re.M))


def test_every_declared_cron_has_a_dispatch_arm():
    """A cron with no arm is a job that silently never runs — the gas-refresh /
    site-baseline / evolution failure."""
    src = _src()
    missing = _declared_crons(src) - _dispatch_arms(src)
    assert not missing, f"declared crons with no dispatch arm: {sorted(missing)}"


def test_every_dispatch_arm_has_a_declared_cron():
    """An arm with no cron is dead code that hides a renamed schedule — the
    market-report '52 14' vs '14:3*' failure."""
    src = _src()
    orphans = _dispatch_arms(src) - _declared_crons(src)
    assert not orphans, f"dispatch arms with no declared cron: {sorted(orphans)}"


def test_dispatch_keys_on_trigger_cron_not_wall_clock():
    """The whole point. If dispatch ever reads the clock again, a delayed run
    silently does nothing."""
    src = _src()
    assert "TRIGGER_CRON: ${{ github.event.schedule }}" in src
    m = re.search(r"- name: Determine which jobs to run(.*?)(?=\n      - name:|\n      # )",
                  src, re.S)
    assert m, "dispatch step not found"
    step = m.group(1)
    for clock in ('case "${HOUR}:00"', 'case "${HOUR}:${MINUTE}"'):
        assert clock not in step, f"dispatch still branches on the wall clock: {clock}"


def test_unmapped_schedule_fails_loudly():
    """The old code fell back to keep-alive, which made a dead job look exactly
    like a healthy idle tick. An unmapped cron must break the build instead."""
    src = _src()
    m = re.search(r"- name: Determine which jobs to run(.*?)(?=\n      - name:|\n      # ★ Land)",
                  src, re.S)
    assert m, "dispatch step not found"
    step = m.group(1)
    assert "::error::" in step, "unmapped cron does not emit a workflow error"
    assert re.search(r'if \[ -z "\$JOBS" \];.*?exit 1', step, re.S), \
        "unmapped cron does not exit non-zero"
    assert 'JOBS="keep-alive"' not in step, \
        "keep-alive fallback reintroduced — this is what hid the dead crons"


def test_the_previously_dead_jobs_are_dispatchable():
    """Regression pin naming the actual casualties, so a future refactor that
    drops one of them fails here with their names on it."""
    src = _src()
    m = re.search(r'case "\$\{TRIGGER_CRON\}" in(.*?)\n\s+esac', src, re.S)
    body = m.group(1)
    for job in ("gas-refresh", "site-baseline", "evolution", "market-report",
                "ambassador", "land-power-sync",
                # 2026-08-07: the keeper ported off the decommissioned
                # heroic-reprieve zombie scheduler — this workflow is now its
                # ONLY driver, so losing an arm kills it outright.
                # (energy-discovery was retired 2026-08-21: dead HIFLD sources;
                # manual-only via the dropdown.)
                "alert-emails"):
        assert job in body, f"{job} lost its dispatch arm"
    assert "energy-discovery" not in body, "energy-discovery arm came back (retired 2026-08-21)"


def _manual_choices(src: str) -> set:
    """Jobs offered in the workflow_dispatch dropdown."""
    m = re.search(r"job:\n(.*?)\n\nenv:", src, re.S)
    assert m, "workflow_dispatch job input not found"
    return {c for c in re.findall(r"^\s+- (\S+)\s*$", m.group(1), re.M)}


def test_a_manual_run_with_no_job_fails_with_its_own_error():
    """#2132 keys dispatch on github.event.schedule, which is EMPTY on
    workflow_dispatch. Without an explicit branch, a manual run with no job
    selected dies on `No dispatch arm for trigger cron []` — blaming a cron that
    was never involved. The input's own description used to promise "leave empty
    for scheduled dispatch", a behaviour that died with the wall clock."""
    src = _src()
    m = re.search(r"- name: Determine which jobs to run(.*?)(?=\n      - name:|\n      # ★ Land)",
                  src, re.S)
    assert m, "dispatch step not found"
    step = m.group(1)
    assert re.search(r'if \[ -z "\$\{TRIGGER_CRON\}" \]', step), (
        "no explicit empty-TRIGGER_CRON branch — a manual run with no job "
        "selected reports an unmapped-cron error naming a cron that never ran")
    assert "leave empty for scheduled dispatch" not in src, (
        "the job input still advertises 'leave empty for scheduled dispatch', "
        "which #2132 made impossible — an empty choice now always exits 1")


def test_every_dispatchable_job_can_also_be_run_by_hand():
    """★ The dropdown drifted from the arms: land-power-sync — the job this
    workflow exists to keep alive — was dispatched on a cron but absent from the
    manual list, so a missed slot could not be backfilled by hand. A manual
    re-run is the ONLY recovery path when GitHub drops a scheduled run, which it
    does routinely (measured 2026-08-02: 1 of 12 heartbeat fires landed in the
    04:00 hour)."""
    src = _src()
    m = re.search(r'case "\$\{TRIGGER_CRON\}" in(.*?)\n\s+esac', src, re.S)
    dispatched = set()
    for arm in re.findall(r'JOBS="([^"]+)"', m.group(1)):
        dispatched.update(j.strip() for j in arm.split(","))
    missing = dispatched - _manual_choices(src)
    assert not missing, (
        f"jobs dispatched on a cron but not offered for manual run: "
        f"{sorted(missing)}")
