"""The hand-maintained agent-facing surfaces must agree with PINNED canon.

WHY CI NEEDS THIS WHEN A RUNTIME SHELL ALREADY CHECKS IT
--------------------------------------------------------
loop-control's `surface_canon` lane does check these files — and it had been
reporting them red. It is a runtime shell: it observes drift after it ships and
files a finding nobody converts. The audit shell's own note is the whole story:

    "llms-full.txt is three canon generations stale
     (15,000+ facilities / 1,500+ deals) — never a heal target"

Never a heal target. These files are hand-maintained, no generator writes them,
and the daily heal that keeps the SERVED copies current does not touch them. So
they drifted for three canon generations while every automated surface stayed
right, and the only thing watching could report but not fix.

Measured 2026-08-31, before this guard:

    facilities   15,000+  (static/llms.txt, static/llms-full.txt, llms.txt,
                           .well-known/mcp.json)   canon 18,500+   UNDER by 3,500
    facilities   12,650+  (dchub-frontend/llms.txt)                UNDER by 5,850
    M&A deals     4,000+  (static/llms.txt)        canon  1,900+   OVER by 2,100
    M&A deals     1,600+  (llms.txt, mcp.json)                     UNDER
    M&A deals     1,400+  (llms.txt)                               UNDER

The same quantity stated three different ways across four files, one of them an
over-claim. An agent reading llms.txt to decide whether to use DC Hub was being
told we track 4,000+ deals in one file and 1,400+ in another.

This guard is CI-side so the drift cannot land, not merely be noticed later.
"""

import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Surfaces an agent or crawler reads directly, that NO generator maintains.
SURFACES = (
    "static/llms.txt",
    "static/llms-full.txt",
    "llms.txt",
    # ★ The repo-ROOT llms-full.txt and README.md were missed on the first pass
    # of this fix and caught by test_pending_facility_surfaces_still_need_fixing
    # — the ratchet that refuses to keep waiving a surface once it is clean.
    # Both are separate files from their static/ near-twins.
    "llms-full.txt",
    "README.md",
    "dchub-frontend/llms.txt",
    ".well-known/mcp.json",
    # ★ Repo-root mcp.json was missed by BOTH the original fix and the first
    # version of this guard, and sat at 15,000+ while every sibling said
    # 18,500+ — which is what kept loop-control's `surfaces_agree` red. The
    # authoritative list is the one loop_control_master_shell scans; this set
    # is kept equal to it by test_this_list_matches_the_shells_own_list below.
    "mcp.json",
    # Listed by the shell but absent from this repo layout — the per-file tests
    # below skip a file that does not exist, and keeping it here keeps the two
    # lists provably equal. If it ever appears, it is covered from day one.
    "static/mcp.json",
)

# Floors that were live-wrong on 2026-08-31 and must never return.
RETIRED_FACILITY_FLOORS = ("15,000+", "12,650+")
RETIRED_DEAL_FLOORS = ("4,000+", "1,600+", "1,400+")

FACILITY_WORDS = ("facilit", "data center", "physical")
DEAL_WORDS = ("m&a", "transaction", "deal", "acquisition")


def _pinned():
    """PINNED without importing the module (it pulls in flask-adjacent deps)."""
    src = (ROOT / "ai_surface_canon.py").read_text(encoding="utf-8")
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "PINNED" for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("PINNED not found in ai_surface_canon.py")


PUBLIC = _pinned()["public"]
CANON_FACILITIES = PUBLIC["facilities"]
CANON_DEALS = PUBLIC["deals"]


def _existing():
    return [rel for rel in SURFACES if (ROOT / rel).exists()]


def test_the_surface_list_is_not_empty():
    """A file rename would otherwise make every test below vacuously pass —
    the exact shape the scan-floor meta-guard exists to prevent."""
    found = _existing()
    assert len(found) >= 7, f"only {len(found)} of {len(SURFACES)} surfaces found: {found}"


@pytest.mark.parametrize("rel", SURFACES)
def test_no_retired_facility_floor_is_served(rel):
    p = ROOT / rel
    if not p.exists():
        pytest.skip(f"{rel} absent")
    text = p.read_text(encoding="utf-8")
    hits = [f for f in RETIRED_FACILITY_FLOORS if f in text]
    assert not hits, (
        f"{rel} carries retired facility floor(s) {hits}; canon is "
        f"{CANON_FACILITIES}. These files are hand-maintained — no heal job "
        f"will fix this for you.")


@pytest.mark.parametrize("rel", SURFACES)
def test_no_retired_deal_floor_is_served(rel):
    p = ROOT / rel
    if not p.exists():
        pytest.skip(f"{rel} absent")
    text = p.read_text(encoding="utf-8")
    hits = [f for f in RETIRED_DEAL_FLOORS if f in text]
    assert not hits, (
        f"{rel} carries retired deal floor(s) {hits}; canon is {CANON_DEALS}. "
        f"4,000+ in particular was an OVER-claim — the direction that costs "
        f"credibility rather than traffic.")


def test_every_surface_states_the_facility_floor_the_same_way():
    """Three files saying three different numbers for one quantity is the
    defect, independent of which number is right."""
    seen = {}
    for rel in _existing():
        text = (ROOT / rel).read_text(encoding="utf-8")
        for line in text.split("\n"):
            low = line.lower()
            if not any(w in low for w in FACILITY_WORDS):
                continue
            for m in re.finditer(r"\b\d{1,3}(?:,\d{3})+\+", line):
                # only facility-scale numbers, not markets/countries/assets
                v = int(m.group(0).replace(",", "").rstrip("+"))
                if 5_000 <= v <= 100_000:
                    seen.setdefault(m.group(0), set()).add(rel)
    assert len(seen) <= 1, (
        f"facility floor stated {len(seen)} different ways across surfaces: "
        + "; ".join(f"{k} in {sorted(v)}" for k, v in sorted(seen.items())))
    if seen:
        assert CANON_FACILITIES in seen, (
            f"surfaces agree on {list(seen)[0]} but canon is {CANON_FACILITIES}")


def test_canon_values_are_what_this_guard_thinks_they_are():
    """If PINNED moves, this file must be re-read rather than silently pinning
    a stale expectation — the failure mode it was written to catch."""
    # ★2026-09-02: 18,500+ -> 20,100+ and 1,900+ -> 2,000+, following PINNED
    # ['public'] onto the live resolver reading (/api/v1/stats facilities =
    # 20,198, deals = 2,069). These stay LITERALS on purpose: this is the one
    # assertion in the file that must NOT derive from ai_surface_canon, because
    # its whole job is to notice that PINNED moved. Deriving it would make it
    # pass forever and the guard would go silent exactly when it is needed.
    # ★ The RETIRED_* lists were re-checked and NOT touched: neither new canon
    # value collides with them (test_retired_lists_do_not_contain_the_current
    # _canon), and 18,500+/1,900+ were canon until today, not the live-wrong
    # floors of 2026-08-31 that those lists exist to ban.
    assert CANON_FACILITIES == "20,100+", (
        f"PINNED facilities moved to {CANON_FACILITIES}. Update the surfaces "
        f"in SURFACES and the RETIRED_* lists, then this assertion.")
    assert CANON_DEALS == "2,000+", (
        f"PINNED deals moved to {CANON_DEALS}. Same drill.")


def test_retired_lists_do_not_contain_the_current_canon():
    """The bug that put surface-truth's three lanes permanently red: a ban that
    also matches the value canon accepts. Never let these lists collide."""
    assert CANON_FACILITIES not in RETIRED_FACILITY_FLOORS
    assert CANON_DEALS not in RETIRED_DEAL_FLOORS


def test_this_list_matches_the_shells_own_list():
    """★ The gap that let mcp.json through. This guard carried its own hand-typed
    SURFACES list, so a file the runtime shell checks but the test does not is
    invisible until the shell goes red — which is exactly what happened.

    loop_control_master_shell._lane_surface_canon holds the authoritative set.
    Every file it scans must be covered here, or the guard is narrower than the
    thing it guards."""
    shell = (ROOT / "routes" / "loop_control_master_shell.py").read_text()
    i = shell.find("candidates = [")
    assert i > 0, "the shell's candidate list moved — re-point this test"
    block = shell[i:shell.index("]", i)]
    theirs = set()
    for m in re.finditer(r'os\.path\.join\(root,\s*([^)]+)\)', block):
        parts = [p.strip().strip('"\'') for p in m.group(1).split(",")]
        theirs.add("/".join(p for p in parts if p))
    missing = {t for t in theirs if t not in set(SURFACES)}
    assert not missing, (
        f"the shell checks these and this guard does not: {sorted(missing)}")
