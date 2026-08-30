#!/usr/bin/env python3
"""Agent-facing surfaces must not restate canon — in EITHER direction.

★ THE BLIND SPOT THIS CLOSES. Every honest-numbers fence in this repo bans
OVER-claims. None of them looks the other way, so a surface that under-sells us
passes every guard indefinitely. Two instances were found on 2026-08-30, both
on surfaces agents actually read, both months old:

    /.well-known/mcp_facts.json   facilities 15,300+ against 19,500+  (27% under)
                                  deals       1,600+ against  2,000+  (25% under)
                                  ...nine figures in all, stale 30 days
    /api/v1/ai-agents.json        news_articles 3,503 against 13,089  (3.7x under)

The second is the sharper lesson: it was not stale, it was counting the wrong
table — the abandoned `news` rather than the live `news_articles` the field is
named for. No amount of over-claim fencing would ever have found either.

★ WHY UNDER-CLAIMING IS A REAL DEFECT, not modesty. These documents are how an
agent decides whether DC Hub can answer its question. Telling it we track 3,503
news articles when we track 13,089, or 15,300 facilities when we track 19,500,
loses the calls that would have been made — silently, with no error anywhere.

★ THE REFERENCE IS CANON'S OWN PHRASE, not the raw live count. canon already
applies the correct floor per field (facilities floor to 100s, countries to
10s, markets to 100s) and floors DOWN on purpose so a citation is never above
reality. Comparing a surface to the raw count would flag legitimate rounding —
markets "300+" against a live 320 is CORRECT, not a 6% under-claim. Comparing
to canon's phrase asks the only question that matters: does this surface say
what canon says?

★ TOLERANCE EXISTS FOR ONE REASON: generated files lag. mcp_facts.json is
rebuilt daily, so it can legitimately sit one floor-step behind canon between
runs. DEFAULT_TOLERANCE_PCT covers that and nothing more — 27% and 3.7x are
nowhere near it.

★ FAIL CLOSED. A surface that cannot be read, or a field that cannot be parsed,
is reported UNMEASURED and fails the run. "Could not check" has never been
"clean" in this repo and is not here either.

Usage:
    python3 scripts/agent_surface_claim_probe.py            # probe production
    python3 scripts/agent_surface_claim_probe.py --self-test  # no network
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import requests

BASE = "https://dchub.cloud"
# ★ A real UA is not optional: Cloudflare answers urllib's default with 1010.
UA = "dchub-claim-probe/1.0 (+https://dchub.cloud)"

CANON_PATH = "/api/v1/canon/phrases"
DEFAULT_TOLERANCE_PCT = 3.0

# surface path -> (container key or None, {surface field: canon field})
SURFACES = {
    "/.well-known/mcp_facts.json": ("numbers", {
        "facilities": "facilities", "deals": "deals",
        "markets": "markets", "countries": "countries"}),
    "/api/v1/ai-agents.json": ("data_coverage", {
        "facilities": "facilities", "countries": "countries",
        "markets": "markets", "deals": "deals"}),
}


def _num(value):
    """First integer in a value: '19,500+' -> 19500. None when absent."""
    m = re.search(r"([\d][\d,]*)", str(value))
    return int(m.group(1).replace(",", "")) if m else None


def compare(published, canon, tolerance_pct=DEFAULT_TOLERANCE_PCT):
    """Return (verdict, gap_pct). Verdicts: ok | under | over | unmeasured.

    Pure, so the self-test can exercise every branch without a network.
    """
    if published is None or canon is None:
        return "unmeasured", None
    if canon == 0:
        return ("ok", 0.0) if published == 0 else ("over", 100.0)
    gap = (canon - published) / canon * 100.0
    if gap > tolerance_pct:
        return "under", gap
    if gap < -tolerance_pct:
        return "over", gap
    return "ok", gap


def _fetch(path):
    # ★ requests, not urllib: regression-lint bans urllib on this repo (#1940),
    # and the UA is not optional either way — Cloudflare answers urllib's
    # default User-Agent with error 1010 before the request reaches an origin.
    resp = requests.get(BASE + path, timeout=30, headers={"User-Agent": UA})
    resp.raise_for_status()
    return resp.json()


def run(tolerance_pct=DEFAULT_TOLERANCE_PCT):
    try:
        canon = _fetch(CANON_PATH)
    except Exception as e:                                    # noqa: BLE001
        print(f"::error::cannot read canon at {CANON_PATH}: {e}. "
              f"Refusing to report surfaces clean without a reference.")
        return 1

    violations, unmeasured, checked = [], [], 0
    for path, (container, fields) in SURFACES.items():
        try:
            doc = _fetch(path)
        except Exception as e:                                # noqa: BLE001
            unmeasured.append(f"{path}: unreadable ({type(e).__name__}: {e})")
            continue
        scope = doc.get(container) if container else doc
        if not isinstance(scope, dict):
            unmeasured.append(f"{path}: no `{container}` object to read")
            continue
        for field, canon_field in fields.items():
            if field not in scope:
                continue          # a surface need not publish every figure
            pub, ref = _num(scope[field]), _num(canon.get(canon_field))
            verdict, gap = compare(pub, ref, tolerance_pct)
            checked += 1
            if verdict == "unmeasured":
                unmeasured.append(f"{path} :: {field}: unparseable "
                                  f"(published={scope[field]!r} canon="
                                  f"{canon.get(canon_field)!r})")
            elif verdict != "ok":
                violations.append(
                    f"{path} :: {field} {verdict.upper()} by {abs(gap):.1f}% — "
                    f"publishes {pub:,} against canon {ref:,}")
            else:
                print(f"  ok        {path} :: {field} {pub:,} vs canon {ref:,} "
                      f"({gap:+.1f}%)")

    for u in unmeasured:
        print(f"  UNMEASURED {u}")
    for v in violations:
        print(f"  VIOLATION  {v}")

    if unmeasured:
        print(f"::error::{len(unmeasured)} surface figure(s) could not be "
              f"measured. Refusing to report clean on a partial probe.")
    if violations:
        print(f"::error::{len(violations)} agent-facing figure(s) disagree with "
              f"canon. UNDER counts: these documents are how an agent decides "
              f"whether we can answer its question, and under-selling loses the "
              f"call with no error anywhere.")
    if not violations and not unmeasured:
        print(f"✓ {checked} agent-facing figure(s) agree with canon "
              f"(tolerance {tolerance_pct}%)")
    return 1 if (violations or unmeasured) else 0


def self_test():
    """Must-fail controls for the comparison itself. No network."""
    cases = [
        # (published, canon, expected verdict, what it stands for)
        (15300, 19500, "under",      "the mcp_facts facilities gap, 2026-08-30"),
        (3503,  13089, "under",      "the ai-agents news_articles gap, 3.7x"),
        (19500, 19700, "ok",         "a daily-generated file one floor-step behind"),
        (19700, 19700, "ok",         "exact agreement"),
        (25000, 19700, "over",       "an over-claim — the direction already fenced"),
        (None,  19700, "unmeasured", "a field that could not be parsed"),
        (19700, None,  "unmeasured", "canon itself unreadable"),
    ]
    bad = []
    for pub, canon, want, why in cases:
        got, _ = compare(pub, canon)
        mark = "ok " if got == want else "FAIL"
        if got != want:
            bad.append(f"{why}: published={pub} canon={canon} "
                       f"expected {want}, got {got}")
        print(f"  {mark} {str(pub):>6} vs {str(canon):>6} -> {got:<10} {why}")
    if bad:
        print("::error::self-test FAILED:\n  " + "\n  ".join(bad))
        return 1
    print(f"✓ self-test: {len(cases)} control(s) behave correctly, "
          f"including both directions and both unmeasured cases")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE_PCT)
    a = ap.parse_args()
    sys.exit(self_test() if a.self_test else run(a.tolerance))
