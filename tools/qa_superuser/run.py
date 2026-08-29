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
from .claims import http_register, plan, register_run_claims
from . import (probe_contract, probe_data, probe_media, probe_mcp,
               probe_registries, probe_relay, probe_retrieval, probe_web)
from .finding import (BLIND, CRITICAL, GAUGE, INFO, MAJOR, PASS, RED,
                      SEAT_NONE, Finding, blind, stable_key, summarize)
from .http import Unreachable, fetch

CANARY_PATH = "/__qa-superuser-canary-this-path-must-not-exist__"

# ★ The registry is module-level so a TEST can dispatch through the same tuple
#   the runner does. It was inline, and that is precisely how `probe_registries`
#   shipped with `def probe()` while every other probe took `probe(findings)`:
#   its unit tests called `pr.probe()` — agreeing with the wrong signature — and
#   nothing anywhere exercised the dispatch. The mismatch was invisible to CI
#   and swallowed at runtime by the except clause below, so the surface was
#   BLIND on every run from the day it merged.
#
#   Every probe here MUST be `probe(findings) -> None|list`, appending to the
#   list it is given. tests/test_qa_superuser.py asserts that against this
#   tuple; keep new probes on the convention rather than widening the test.
PROBES = (("mcp", probe_mcp), ("web", probe_web),
          ("data", probe_data), ("media", probe_media),
          ("contract", probe_contract),
          ("retrieval", probe_retrieval),
          ("registries", probe_registries),
          # relay runs LAST on purpose: its presence check wants FLAGSHIP_TOOL's
          # (ip, tool, day) budget already spent by the mcp probes so the call
          # lands on the GATED path. Reordering degrades it to a GAUGE, never
          # a false RED — but keep it last to keep the check observing.
          ("relay", probe_relay))


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

    for name, mod in PROBES:
        before = len(findings)
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
                why=f"{type(e).__name__}: {e}", basis=f"probe_{name}.probe()",
                instrument_fault=True))
        else:
            # ★ A probe that returns cleanly having said NOTHING is the quietest
            #   way this harness can go blind: no exception, no verdict, and a
            #   board that shrinks by one surface while still reading "0 red".
            #   `registries` failed loudly (TypeError) and still sat unnoticed
            #   for two days; a silent version would never have been noticed at
            #   all. Contributing zero findings is therefore itself a finding.
            if len(findings) == before:
                findings.append(blind(
                    key=stable_key(name, "surface"), surface=name,
                    seat=SEAT_NONE,
                    title=f"{name} probe produced no verdict at all — "
                          "instrument fault, not a platform verdict",
                    why=f"probe_{name}.probe() returned normally and appended "
                        "0 finding(s); the surface was not measured this run",
                    basis=f"len(findings) before/after probe_{name}.probe()",
                    instrument_fault=True))
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


def claims_for(findings, planned_only: bool) -> dict:
    """★ lane 6 (qa-as-claims). A PASS that names a re-measurable instrument
    registers as a claim with a HORIZON, so a green is re-asked on a clock
    instead of standing until someone remembers to re-run this harness.

    Gated exactly like actuate() below: registering is a WRITE, so --no-actuate
    and DRY_RUN plan the registration and leave the ledger alone.

    ★ AN UNTRUSTWORTHY RUN MINTS NO CLAIMS, and that is load-bearing rather than
    incidental: when the canary did not fire, invalidate() has already demoted
    every PASS to BLIND, so plan() finds nothing backed. A harness that could
    not prove itself must not be able to register reassurance.

    ★ A MISSING KEY IS REPORTED AS A MISSING KEY. Returning `registered: 0`
    when the credential is absent would read as "there was nothing to
    register", which is the exact silent-green this lane exists to end.
    """
    if planned_only:
        out = plan(findings)
        out.pop("_backed_findings", None)
        out.update({"registered": None, "already": None, "refused": [],
                    "errors": [],
                    "note": "planned only — the ledger was not touched"})
        return out
    register = http_register()
    if register is None:
        out = plan(findings)
        out.pop("_backed_findings", None)
        out.update({"registered": None, "already": None, "refused": [],
                    "errors": ["no admin key in the environment — nothing was "
                               "registered; this is a MISSING CREDENTIAL, not "
                               "a run that had no claims to make"]})
        return out
    return register_run_claims(findings, register=register)


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
    claims = claims_for(findings, args.no_actuate or C.DRY_RUN)
    run = {
        "generated_at": started.isoformat(),
        "canary_fired": fired,
        "edge": C.EDGE,
        "counts": counts,
        "claims": claims,
        "findings": [f.to_dict() for f in findings],
    }

    if args.json:
        print(json.dumps(run, indent=2))
    else:
        print(f"QA super-user — {started.isoformat()}  ({C.EDGE})")
        print(f"canary_fired={fired}  " + "  ".join(
            f"{k}={v}" for k, v in counts.items()))
        print(f"claims: registered={claims.get('registered')} "
              f"already={claims.get('already')} "
              f"backed={len(claims.get('backed') or [])}/{claims.get('passes')} "
              f"passes  coverage_of_passes={claims.get('coverage_of_passes')}")
        if claims.get("refused"):
            print(f"        refused: {claims['refused']}")
        if claims.get("errors"):
            print(f"        errors:  {claims['errors']}")
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
