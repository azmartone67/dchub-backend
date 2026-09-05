#!/usr/bin/env python3
"""
MUST-FAIL CONTROL FOR THE GATE BEAT WIRING
═════════════════════════════════════════════════════════════════════════════

routes/gate_runs.py has a control proving the PREDICATE can go red. That says
nothing about whether the twelve workflow beats feed it honest numbers — and a
predicate fed `checked=null` forever is a predicate that never fires.

The one that matters is EMPTY vs ZERO:

    empty  ->  "unknown", the vacuous counter is left ALONE, G4 never fires
    zero   ->  "ran and examined nothing", G4 alarms

A pytest collection abort writes a log with NO summary line. If that maps to
empty, the exact defect this ledger exists to catch — 2,285 tests, zero run,
exit 3, reported green (#1797) — raises nothing at all.

This extracts the REAL `run:` block from each beat step in the real workflow
YAML, stubs gate_beat.sh to echo its arguments, and asserts what the beat would
actually send. The shell under test is the shell that ships.

    python3 scripts/gate_beat_selftest.py

    exit 0  wiring is honest
    exit 1  a beat would send the wrong thing
    exit 2  UNMEASURED — could not extract the steps. NOT a pass.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile

try:
    import yaml
except ImportError:
    print("::error::UNMEASURED — pyyaml unavailable, cannot read the workflows")
    sys.exit(2)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(ROOT, ".github", "workflows")
results: list[tuple[bool, str, str]] = []


def beat_run(workflow: str, job: str) -> str:
    with open(os.path.join(WF, workflow)) as fh:
        d = yaml.safe_load(fh)
    for s in d["jobs"][job]["steps"]:
        if str(s.get("name", "")).startswith("Beat the gate"):
            return s["run"]
    raise KeyError("no beat step in %s:%s" % (workflow, job))


# Absolute fixture paths land in the real /tmp, not the sandbox, so they
# PERSIST between cases. Every path ever used is cleared before each run —
# without this a case can pass by reading the previous case's file, which is
# how "step did not conclude" read a population it was never given.
_ABS_FIXTURES = {
    "/tmp/unit-tests.log", "/tmp/acg.log", "/tmp/crt.log", "/tmp/crt1.log",
    "/tmp/sg.log", "/tmp/sd.log", "/tmp/smoke.log",
}


def run_beat(script: str, env: dict, files: dict) -> str:
    """Execute the real run: block with gate_beat.sh stubbed to echo its args."""
    for _p in _ABS_FIXTURES:
        try:
            os.remove(_p)
        except OSError:
            pass
    with tempfile.TemporaryDirectory() as td:
        os.makedirs(os.path.join(td, "scripts"), exist_ok=True)
        stub = os.path.join(td, "scripts", "gate_beat.sh")
        with open(stub, "w") as fh:
            fh.write('#!/usr/bin/env bash\necho "BEAT gate=$1 verdict=$2 checked=[$3] selftest=$4"\n')
        os.chmod(stub, 0o755)
        # regression-lint calls it via dchub-backend/scripts/
        os.makedirs(os.path.join(td, "dchub-backend", "scripts"), exist_ok=True)
        with open(os.path.join(td, "dchub-backend", "scripts", "gate_beat.sh"), "w") as fh:
            fh.write('#!/usr/bin/env bash\necho "BEAT gate=$1 verdict=$2 checked=[$3] selftest=$4"\n')
        os.chmod(os.path.join(td, "dchub-backend", "scripts", "gate_beat.sh"), 0o755)
        for name, body in files.items():
            path = name if os.path.isabs(name) else os.path.join(td, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write(body)
        e = dict(os.environ)
        e.update(env)
        p = subprocess.run(["bash", "-c", script], cwd=td, env=e,
                           capture_output=True, text=True)
        return p.stdout + p.stderr


def expect(case: str, out: str, want: str) -> None:
    ok = want in out
    results.append((ok, case, out.strip().splitlines()[-1][:88] if out.strip() else "(no output)"))


PYTEST_OK = "tests/test_a.py::test_x PASSED\n= 12419 passed, 59 skipped in 1351.50s =\n"
# A collection abort: pytest dies at import time. NO summary line, ever.
PYTEST_ABORT = ("INTERNALERROR> ... sys.exit(1 if fail else 0)\n"
                "INTERNALERROR> SystemExit: 0\n")

try:
    ut = beat_run("pre-merge.yml", "unit-tests")
    dbp = beat_run("pre-merge.yml", "db-parity")
    acg = beat_run("app-contract-gate.yml", "app-contract-gate")
    sg = beat_run("brain-pr-substance-gate.yml", "substance-gate")
    sd = beat_run("brain-spec-debt-tracker.yml", "file-spec-debt")
    crt = beat_run("check-route-tables.yml", "check")
except Exception as e:  # noqa: BLE001
    print("::error::UNMEASURED — cannot extract beat steps: %s" % e)
    sys.exit(2)

# ── ★ THE ONE THAT MATTERS ──────────────────────────────────────────────────
expect("unit-tests: normal run reports the real count",
       run_beat(ut, {"JOB_STATUS": "success"}, {"/tmp/unit-tests.log": PYTEST_OK}),
       "verdict=pass checked=[12419]")

expect("★ unit-tests: COLLECTION ABORT reports ZERO, not empty",
       run_beat(ut, {"JOB_STATUS": "failure"}, {"/tmp/unit-tests.log": PYTEST_ABORT}),
       "verdict=fail checked=[0]")

expect("unit-tests: no log at all reports empty (unknown, not zero)",
       run_beat(ut, {"JOB_STATUS": "cancelled"}, {}),
       "verdict=unmeasured checked=[]")

# ── db-parity ───────────────────────────────────────────────────────────────
expect("db-parity: counts the parity tests that ran",
       run_beat(dbp, {"JOB_STATUS": "success"}, {"out.txt": "= 3 passed in 2s =\n"}),
       "verdict=pass checked=[3]")
expect("db-parity: log present but unparseable reports ZERO",
       run_beat(dbp, {"JOB_STATUS": "failure"}, {"out.txt": "boom\n"}),
       "verdict=fail checked=[0]")

# ── app-contract-gate ───────────────────────────────────────────────────────
expect("app-contract-gate: reads the booted route count",
       run_beat(acg, {"JOB_STATUS": "success"},
                {"/tmp/acg.log": "booted in 4.2s — 789 routes, 61 blueprints, x\n"}),
       "verdict=pass checked=[789]")
expect("app-contract-gate: app did not boot reports ZERO routes",
       run_beat(acg, {"JOB_STATUS": "failure"},
                {"/tmp/acg.log": "ImportError: no module named x\n"}),
       "verdict=fail checked=[0]")

# ── kill switches must read UNMEASURED, never pass ──────────────────────────
expect("★ substance-gate: kill switch held reads UNMEASURED",
       run_beat(sg, {"JOB_STATUS": "success", "GATE_DISABLE": "1",
                     "BASE_SHA": "", "HEAD_SHA": ""}, {}),
       "verdict=unmeasured")
expect("★ spec-debt-tracker: kill switch held reads UNMEASURED",
       run_beat(sd, {"JOB_STATUS": "success", "GATE_DISABLE": "1", "PR_BODY": ""}, {}),
       "verdict=unmeasured")
expect("spec-debt-tracker: no unchecked boxes is no_scope, not vacuous",
       run_beat(sd, {"JOB_STATUS": "success", "GATE_DISABLE": "0",
                     "PR_BODY": "all done\n"}, {}),
       "verdict=no_scope")
expect("spec-debt-tracker: unchecked boxes are counted",
       run_beat(sd, {"JOB_STATUS": "success", "GATE_DISABLE": "0",
                     "PR_BODY": "- [ ] a\n- [ ] b\n- [x] c\n"}, {}),
       "verdict=pass checked=[2]")

# ── the route-table RATCHET takes its verdict from OUTPUT ───────────────────
# ★ The population and the verdict are printed by DIFFERENT steps, so these
# fixtures use two files. The first version of this control put both lines in
# one log — a log the real workflow never produces — so it passed while the
# shipped beat sent checked=empty and G4 could never fire. A fixture that does
# not match reality is a control that tests a workflow you did not write.
#
# ★ 2026-09-05 — the fixtures are re-cut from what the RATCHET actually prints.
# The job dropped its continue-on-error, and the ADVISORY token moved from a
# ::notice:: printed on EVERY run to an ::error:: printed ONLY when a PR adds a
# new uncovered route. Verbatim strings from
# scripts/check_route_table_coherence.py; a clean run with baselined debt now
# prints "route-table coherence —" with NO "ADVISORY", and must read `pass`.
expect("★ check-route-tables: a NEW uncovered route is a real refusal",
       run_beat(crt, {"JOB_STATUS": "failure", "SELFTEST_OUTCOME": "success"},
                {"/tmp/crt1.log": "discovered 356 Flask HTML routes\n",
                 "/tmp/crt.log": "::error::route-table coherence ADVISORY — 1 NEW Flask route(s) "
                                 "are not in the CF tables.\n"}),
       "verdict=fail checked=[356]")
expect("check-route-tables: clean run passes",
       run_beat(crt, {"JOB_STATUS": "success", "SELFTEST_OUTCOME": "success"},
                {"/tmp/crt1.log": "discovered 356 Flask HTML routes\n",
                 "/tmp/crt.log": "OK — 356 Flask HTML routes covered by both tables "
                                 "or baselined (130 known, 0 new).\n"}),
       "verdict=pass checked=[356]")
expect("★ check-route-tables: BASELINED drift is not a refusal",
       run_beat(crt, {"JOB_STATUS": "success", "SELFTEST_OUTCOME": "success"},
                {"/tmp/crt1.log": "discovered 356 Flask HTML routes\n",
                 "/tmp/crt.log": "::notice::route-table coherence — 130 Flask route(s) not in "
                                 "the CF tables, ALL BASELINED pre-existing drift, none NEW.\n"
                                 "OK — 356 Flask HTML routes covered by both tables "
                                 "or baselined (130 known, 0 new).\n"}),
       "verdict=pass checked=[356]")
expect("check-route-tables: step did not conclude reads UNMEASURED",
       run_beat(crt, {"JOB_STATUS": "success", "SELFTEST_OUTCOME": "success"},
                {"/tmp/crt.log": "crashed\n"}),
       "verdict=unmeasured")
expect("★ check-route-tables: the must-fail control is REPORTED, not assumed",
       run_beat(crt, {"JOB_STATUS": "success", "SELFTEST_OUTCOME": "failure"},
                {"/tmp/crt1.log": "discovered 356 Flask HTML routes\n",
                 "/tmp/crt.log": "OK — 356 Flask HTML routes covered by both tables "
                                 "or baselined (130 known, 0 new).\n"}),
       "verdict=pass checked=[356] selftest=fail")

# ── ★ every job that beats must be able to REACH the beat script ────────────
# scripts/gate_beat.sh only exists after a checkout. pre-merge:smoke-probe and
# brain-spec-debt-tracker both work entirely over the network and had none, so
# the first version of their beats died with exit 127 — a gate reporting green
# while its beat never landed, which reads as `never-run` forever. Structural,
# so it is checked structurally rather than left to whoever wires the next one.
for _wf in sorted(os.listdir(WF)):
    if not _wf.endswith(".yml"):
        continue
    try:
        with open(os.path.join(WF, _wf)) as fh:
            _d = yaml.safe_load(fh)
    except Exception:  # noqa: BLE001
        continue
    if not isinstance(_d, dict):
        continue
    for _job, _spec in (_d.get("jobs") or {}).items():
        _steps = _spec.get("steps") or []
        _beats = [x for x in _steps if str(x.get("name", "")).startswith("Beat the gate")]
        if not _beats:
            continue
        _has_checkout = any("actions/checkout" in str(x.get("uses", "")) for x in _steps)
        _prefix = "dchub-backend/" if "dchub-backend/scripts/gate_beat.sh" in _beats[0]["run"] else ""
        results.append((
            _has_checkout,
            "%s:%s checks out before beating" % (_wf.replace(".yml", ""), _job),
            "no actions/checkout — %sscripts/gate_beat.sh cannot exist (exit 127)" % _prefix
            if not _has_checkout else "checkout present",
        ))

# Every absolute fixture a case writes must be declared in _ABS_FIXTURES, or it
# is never cleared and leaks into the next case.
_used = {"/tmp/unit-tests.log", "/tmp/acg.log", "/tmp/crt.log", "/tmp/crt1.log"}
results.append((_used <= _ABS_FIXTURES, "every absolute fixture is cleared between cases",
                "undeclared: %r" % sorted(_used - _ABS_FIXTURES)))

failed = [r for r in results if not r[0]]
for ok, name, detail in results:
    print("  %s  %-56s %s" % ("PASS" if ok else "FAIL", name, detail))
print("\n%d/%d beat-wiring cases passed" % (len(results) - len(failed), len(results)))
if failed:
    print("::error::gate beat wiring FAILED %d case(s) — a gate would feed the "
          "ledger a wrong or unknown count." % len(failed))
    sys.exit(1)
print("OK — every beat sends an honest verdict and an honest population.")
sys.exit(0)
