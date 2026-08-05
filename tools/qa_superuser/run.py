#!/usr/bin/env python3
"""The QA super-user runner: exercise every surface, then prove the run counted.

Order matters. The CANARY runs first and last-word: it is a check that MUST come
back RED, asserted against a real surface. If it does not, this harness cannot
report failure, and a harness that cannot report failure reporting "all green" is
the single most dangerous output in this codebase — a silently-empty test suite
shipped twice on 2026-07-28 and left the backend with no gate at all for hours,
rendered as an ordinary green job.

So when the canary fails to fire, the run does not publish a green board. It
publishes BLIND, loudly, and says why.

Usage:
    python3 -m tools.qa_superuser.run              # probe, diff, actuate
    QA_DRY_RUN=1 python3 -m tools.qa_superuser.run # probe + print, touch nothing
    python3 -m tools.qa_superuser.run --json       # machine-readable to stdout
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys

from . import config as C
from . import (probe_contract, probe_data, probe_media, probe_mcp,
               probe_web)
from .finding import (BLIND, CRITICAL, GAUGE, INFO, MAJOR, PASS, RED,
                      SEAT_NONE, Finding, blind, stable_key, summarize)
from .http import Unreachable, fetch

CANARY_PATH = "/__qa-superuser-canary-this-path-must-not-exist__"


def run_canary() -> tuple[bool, str]:
    """A control that MUST produce a failure. Returns (fired, evidence).

    Asserts something known-false about a real surface — that a deliberately
    absent path returns 2xx. If the machinery is intact this "check" fails, which
    is the proof we want: failures can travel from a probe to the board.

    Note it exercises the real transport rather than raising in Python. A
    self-assertion would prove only that ``assert`` works; this proves the whole
    path from HTTP through verdict construction is capable of producing a RED.
    """
    url = f"{C.EDGE}{CANARY_PATH}"
    try:
        status, _h, _b = fetch(url, timeout=C.HTTP_TIMEOUT, retries=1)
    except Unreachable as e:
        return False, f"canary could not reach {url}: {e}"
    # The control expectation is deliberately WRONG: we "expect" 200 from a path
    # that must not exist. A correct platform makes this expectation fail.
    fired = status >= 400
    note = ("as required — a 4xx proves the harness can observe a negative and "
            "carry it through to a verdict"
            if fired else
            "UNEXPECTED 2xx from a path that must not exist")
    return fired, f"GET {CANARY_PATH} -> HTTP {status} ({note})"


def collect() -> tuple[list[Finding], bool]:
    """Run every probe. Returns (findings, canary_fired)."""
    findings: list[Finding] = []

    fired, evidence = run_canary()
    findings.append(Finding(
        key=stable_key("harness", "canary"), surface="harness", seat=SEAT_NONE,
        title=("Must-fail control fired — this run can report failures"
               if fired else
               "MUST-FAIL CONTROL DID NOT FIRE — this run cannot be trusted"),
        verdict=PASS if fired else RED,
        severity=INFO if fired else CRITICAL,
        evidence=evidence,
        basis=f"GET {C.EDGE}{CANARY_PATH} with a deliberately wrong expectation "
              "(that an absent path returns 2xx), exercising the real transport "
              "and verdict path rather than a bare Python assertion",
        red_when="the control does not produce a failure — meaning this harness "
                 "is structurally incapable of reporting one, and every green "
                 "result in this run is unproven",
        remedy="Treat the whole run as BLIND. A silently-green harness is worse "
               "than no harness: it manufactures confidence."))

    for name, mod in (("mcp", probe_mcp), ("web", probe_web),
                      ("data", probe_data), ("media", probe_media),
                      ("contract", probe_contract)):
        try:
            mod.probe(findings)
        except Unreachable as e:
            findings.append(blind(
                key=stable_key(name, "surface"), surface=name, seat=SEAT_NONE,
                title=f"{name} surface unobserved", why=str(e),
                basis=f"probe_{name}.probe()"))
        except Exception as e:  # noqa: BLE001
            # A crashing probe is a broken instrument, never a broken platform.
            findings.append(blind(
                key=stable_key(name, "surface"), surface=name, seat=SEAT_NONE,
                title=f"{name} probe crashed — instrument fault, not a platform verdict",
                why=f"{type(e).__name__}: {e}", basis=f"probe_{name}.probe()"))
    return findings, fired


def invalidate(findings: list[Finding]) -> list[Finding]:
    """Demote every PASS to BLIND when the canary did not fire.

    A RED stays RED — an observed failure is still evidence even from a suspect
    harness, and suppressing it would be the same mistake in the other direction.
    What we refuse to publish is REASSURANCE we cannot back.
    """
    out = []
    for f in findings:
        if f.verdict == PASS and f.surface != "harness":
            out.append(blind(
                key=f.key, surface=f.surface, seat=f.seat, title=f.title,
                why="the run's must-fail control did not fire, so this PASS is "
                    "unproven and is reported as unobserved rather than green",
                basis=f.basis))
        else:
            out.append(f)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="DC Hub QA super-user")
    ap.add_argument("--json", action="store_true", help="emit JSON to stdout")
    ap.add_argument("--no-actuate", action="store_true",
                    help="probe and print; do not touch the board or any issue")
    args = ap.parse_args(argv)

    started = datetime.datetime.now(datetime.timezone.utc)
    findings, fired = collect()
    if not fired:
        findings = invalidate(findings)

    findings.sort(key=lambda f: f.rank)
    counts = summarize(findings)
    run = {
        "generated_at": started.isoformat(),
        "canary_fired": fired,
        "edge": C.EDGE,
        "counts": counts,
        "findings": [f.to_dict() for f in findings],
    }

    if args.json:
        print(json.dumps(run, indent=2))
    else:
        print(f"QA super-user — {started.isoformat()}  ({C.EDGE})")
        print(f"canary_fired={fired}  " + "  ".join(
            f"{k}={v}" for k, v in counts.items()))
        print()
        for f in findings:
            print(f"[{f.verdict:5}] {f.severity:8} {f.surface:7} {f.seat:5} {f.title}")
            print(f"          evidence: {f.evidence}")
            if f.verdict == RED:
                print(f"          red_when: {f.red_when}")
                if f.remedy:
                    print(f"          remedy:   {f.remedy}")
            print()

    if not args.no_actuate and not C.DRY_RUN:
        from .board import actuate
        actuate(run)
    elif not args.json:
        print("(dry run — board and issue untouched)")

    # Exit non-zero ONLY when the harness itself is untrustworthy. A RED finding
    # is the tool working correctly and must not fail the workflow, or the very
    # first real defect switches the watcher off.
    return 0 if fired else 2


if __name__ == "__main__":
    sys.exit(main())
