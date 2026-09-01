#!/usr/bin/env python3
"""
MUST-FAIL CONTROL FOR THE GATE LIVENESS LEDGER
═════════════════════════════════════════════════════════════════════════════

The ledger exists to catch gates that cannot fail. It would be absurd for the
ledger itself to be one — and it is the likeliest candidate, because four
measurement surfaces on 2026-08-17 published wrong numbers and THREE of them
ran in the OPPOSITE direction from the bug each was built to catch. A board
that reports every gate healthy because its own predicate is broken is exactly
the failure it claims to detect.

So this asserts, on every CI run, that routes.gate_runs.evaluate_gate() still
goes red for each defect class it claims to catch — and that it does NOT fire
on a healthy row.

Every case calls the REAL evaluate_gate(). Only the ledger row is injected, so
a mutation lands on the data, not on a paraphrase of the logic under test.

    python3 scripts/gate_runs_selftest.py

    exit 0  the control is healthy
    exit 1  the CONTROL failed — a planted defect went undetected, or a healthy
            row was flagged. The ledger is not trustworthy.
    exit 2  UNMEASURED — evaluate_gate could not be imported. NOT a pass.
"""
from __future__ import annotations

import datetime
import os
import sys
import types

PASS, FAIL, UNMEASURED = 0, 1, 2

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)


def _stub_deps() -> None:
    """Let the PURE predicate be imported without flask/psycopg2 present.

    routes/gate_runs.py touches both only at import time (Blueprint construction
    and route decorators). Stubbing them keeps this control runnable in a
    stdlib-only job, the same reason api_response_contract.py refuses
    third-party imports: a control must not be able to fail for a reason
    unrelated to the thing it controls.
    """
    if "flask" not in sys.modules:
        try:
            import flask  # noqa: F401
        except ImportError:
            f = types.ModuleType("flask")

            class _BP:
                def __init__(self, *a, **k):
                    pass

                def route(self, *a, **k):
                    return lambda fn: fn

            f.Blueprint = _BP
            f.jsonify = lambda *a, **k: None
            f.request = None
            sys.modules["flask"] = f
    if "psycopg2" not in sys.modules:
        try:
            import psycopg2  # noqa: F401
        except ImportError:
            p = types.ModuleType("psycopg2")
            p.connect = lambda *a, **k: None
            sys.modules["psycopg2"] = p


_stub_deps()

try:
    from routes.gate_runs import evaluate_gate, GATE_REGISTRY
except Exception as e:  # noqa: BLE001
    print("::error::UNMEASURED — cannot import routes.gate_runs.evaluate_gate: %s" % e)
    sys.exit(UNMEASURED)

NOW = datetime.datetime(2026, 8, 31, 12, 0, tzinfo=datetime.timezone.utc)
_results: list[tuple[bool, str, str]] = []


def healthy() -> dict:
    """A gate in perfect health: ran an hour ago, examined 412 items, passed,
    has a must-fail control, and refused something last week."""
    return {
        "last_run": NOW - datetime.timedelta(hours=1),
        "last_verdict": "pass",
        "last_refusal": NOW - datetime.timedelta(days=7),
        "refusals_total": 3,
        "last_checked_n": 412,
        "consecutive_vacuous": 0,
        "selftest": "pass",
        "cadence_hours": 48,
        "first_seen": NOW - datetime.timedelta(days=200),
    }


def case(name: str, mutate: dict, *, want_alarm: str = "", want_advisory: str = "",
         want_clean: bool = False) -> None:
    """Plant one defect and assert evaluate_gate reacts as claimed.

    ★ The mutation is asserted to have APPLIED before anything is concluded from
    the result. `git checkout` silently no-ops on an untracked file and `|| true`
    hides it, which is exactly how a corrupted-source run once read as a passing
    control (2026-08-19). A key that does not change the row is a broken case,
    not a passing one.
    """
    rec = healthy()
    for k, v in mutate.items():
        if k in rec and rec[k] == v:
            _results.append((False, name, "MUTATION DID NOT APPLY: %s already == %r" % (k, v)))
            return
        rec[k] = v

    alarms, advisories = evaluate_gate(rec, NOW)
    blob_a, blob_v = " | ".join(alarms).lower(), " | ".join(advisories).lower()

    if want_clean:
        ok = not alarms
        _results.append((ok, name, "expected no alarms, got %r" % (alarms,) if not ok else "clean"))
        return
    if want_alarm:
        ok = any(want_alarm.lower() in a.lower() for a in alarms)
        _results.append((ok, name, "expected an ALARM mentioning %r; alarms=%r advisories=%r"
                         % (want_alarm, alarms, advisories) if not ok else blob_a[:70]))
        return
    if want_advisory:
        ok = (any(want_advisory.lower() in a.lower() for a in advisories)
              and not any(want_advisory.lower() in a.lower() for a in alarms))
        _results.append((ok, name, "expected an ADVISORY (not an alarm) mentioning %r; "
                         "alarms=%r advisories=%r" % (want_advisory, alarms, advisories)
                         if not ok else blob_v[:70]))


# ── 0. THE VACUITY CONTROL ───────────────────────────────────────────────────
# If a healthy row alarms, every case below "passes" for the wrong reason.
case("healthy row raises nothing", {"note": "x"}, want_clean=True)

# ── 1. G1 never ran ──────────────────────────────────────────────────────────
case("G1 never ran", {"last_run": None}, want_alarm="never ran")

# ── 2. G2 stale beyond 2x cadence ────────────────────────────────────────────
case("G2 stale (>2x cadence)",
     {"last_run": NOW - datetime.timedelta(hours=200)}, want_alarm="cadence")

case("G2 does NOT fire inside 2x cadence",
     {"last_run": NOW - datetime.timedelta(hours=95)}, want_clean=True)

# ── 3. G3 unmeasured ─────────────────────────────────────────────────────────
case("G3 unmeasured is not a pass",
     {"last_verdict": "unmeasured"}, want_alarm="unmeasured")

case("G3 unknown verdict is caught",
     {"last_verdict": "greenish"}, want_alarm="unknown")

# ── 4. G4 vacuous pass ───────────────────────────────────────────────────────
case("G4 examined zero items",
     {"last_checked_n": 0}, want_alarm="vacuous")

case("G4 three consecutive vacuous runs",
     {"consecutive_vacuous": 3}, want_alarm="examined nothing")

# ── 5. no_scope is an AFFIRMATIVE zero, not a vacuous one ────────────────────
# Without this the feed board's eia-pricing/osm-crawl false red repeats here:
# a delta gate on a PR touching no Python is healthy, not broken.
case("no_scope + zero checked stays clean",
     {"last_verdict": "no_scope", "last_checked_n": 0}, want_clean=True)

# ── 6. G5 unproven — ADVISORY while DCHUB_GATE_G5_BLOCKS is unset ────────────
case("G5 absent control advises, does not alarm",
     {"selftest": "absent"}, want_advisory="unproven")

case("G5 failing control advises",
     {"selftest": "fail"}, want_advisory="must-fail control is fail")

# ── 7. G6 never fired — ADVISORY, permanently ────────────────────────────────
case("G6 never refused anything advises",
     {"refusals_total": 0, "last_refusal": None}, want_advisory="never refused")

case("G6 does NOT fire on a young gate",
     {"refusals_total": 0, "last_refusal": None,
      "first_seen": NOW - datetime.timedelta(days=10)}, want_clean=True)

# ── 8. ★★★ THE INVERTED SEMANTIC ─────────────────────────────────────────────
# verdict='fail' means the gate REFUSED something. It did its job. If someone
# "fixes" _OK_VERDICT to treat fail as an alarm, the whole board inverts and
# every working gate reads broken. This case is the fence around that.
case("verdict=fail is HEALTHY (the gate refused something)",
     {"last_verdict": "fail", "last_refusal": NOW}, want_clean=True)

# ── 9. the registry is non-empty and job-granular ────────────────────────────
_results.append((
    len(GATE_REGISTRY) >= 8 and all(":" in g for g in GATE_REGISTRY),
    "registry is populated and job-granular",
    "GATE_REGISTRY=%d entries, non-job keys=%r"
    % (len(GATE_REGISTRY), [g for g in GATE_REGISTRY if ":" not in g]),
))


failed = [r for r in _results if not r[0]]
for ok, name, detail in _results:
    print("  %s  %-52s %s" % ("PASS" if ok else "FAIL", name, detail))
print("\n%d/%d control cases passed" % (len(_results) - len(failed), len(_results)))

if failed:
    print("::error::the gate-ledger must-fail control FAILED — %d case(s). The ledger "
          "cannot be trusted to report gate health until this is green." % len(failed))
    sys.exit(FAIL)
print("OK — evaluate_gate still goes red for every class it claims to catch.")
sys.exit(PASS)
