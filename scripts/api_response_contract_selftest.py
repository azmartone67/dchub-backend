#!/usr/bin/env python3
"""
SELF-TEST FOR THE API RESPONSE CONTRACT GUARD
═════════════════════════════════════════════════════════════════════════════

A guard that cannot fail is worse than no guard: it converts "nobody checked"
into "checked, all clear". This asserts, ON EVERY CI RUN, that the guard
still goes red for each defect class it claims to catch — and, just as
importantly, that it does NOT pass vacuously when the surface is empty or
implausibly small.

Every case calls api_response_contract.check() — the REAL diff path the CI
step runs. The only thing injected is the "current" surface, so a mutation is
applied to the data, not to the logic under test.

    python3 scripts/api_response_contract_selftest.py     # exit 0 = healthy

Exit 1 means the GUARD is broken (it failed to detect a planted defect, or it
flagged a benign one), regardless of whether the API itself is fine.
"""
from __future__ import annotations

import copy
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import api_response_contract as G  # noqa: E402

PASS, FAIL, UNMEASURED = 0, 1, 2
_NAMES = {0: "PASS", 1: "FAIL", 2: "UNMEASURED"}

_results: list[tuple[bool, str, str]] = []


def expect(case: str, want: int, got: int, must_mention: str = "",
           output: str = "") -> None:
    ok = (got == want)
    detail = f"want {_NAMES[want]}({want}), got {_NAMES.get(got, got)}({got})"
    if ok and must_mention and must_mention.lower() not in output.lower():
        ok = False
        detail += f"; verdict right but message never mentions {must_mention!r}"
    _results.append((ok, case, detail))
    print(f"  {'ok  ' if ok else 'FAIL'}  {case}\n          {detail}")


def run(baseline: dict, current: dict) -> tuple[int, str]:
    """Run the real check() with an injected current surface."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(baseline, fh)
        path = fh.name
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = G.check(baseline_path=path, verbose=True, surface=current)
        return code, buf.getvalue()
    finally:
        os.unlink(path)


def run_verify(baseline: dict, current: dict) -> tuple[int, str]:
    """Run the real verify_baseline() with an injected current surface."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(baseline, fh)
        path = fh.name
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = G.verify_baseline(baseline_path=path, verbose=True,
                                     surface=current)
        return code, buf.getvalue()
    finally:
        os.unlink(path)


def first_strict_key(surface: dict) -> tuple[str, str]:
    """An endpoint + key whose removal must produce a hard FAIL (parent level
    is not `open`, so the key genuinely cannot be hiding)."""
    for eid, rec in sorted(surface["endpoints"].items()):
        if rec["resolution"] != "resolved" or "" in rec["open_at"]:
            continue
        for k in rec["keys"]:
            if "." not in k and "[]" not in k:
                return eid, k
    raise SystemExit("self-test setup failed: no strictly-protected key found")


def main() -> int:
    print("═" * 74)
    print("API RESPONSE CONTRACT GUARD — SELF-TEST")
    print("═" * 74)

    base = G.extract_surface()
    if not base["endpoints"]:
        print("self-test setup failed: extractor produced an empty surface")
        return 1
    eid, key = first_strict_key(base)
    print(f"  mutation target: {eid}  key '{key}'\n")

    # ── 1. IDENTITY — unmutated surface must PASS. If this fails, every
    #       other 'red' below is meaningless (it would be red for any input).
    code, out = run(base, copy.deepcopy(base))
    expect("identity: unchanged surface PASSes", PASS, code, "PASS", out)

    # ── 2. KEY REMOVED — the core defect. Must go red.
    mut = copy.deepcopy(base)
    mut["endpoints"][eid]["keys"] = [k for k in mut["endpoints"][eid]["keys"]
                                     if k != key]
    code, out = run(base, mut)
    expect("key removed -> FAIL", FAIL, code, key, out)

    # ── 3. KEY RENAMED — the /api/ai-ecosystem/status class (platforms_count
    #       served, active_platforms read). Must go red AND be named a rename.
    mut = copy.deepcopy(base)
    ks = mut["endpoints"][eid]["keys"]
    mut["endpoints"][eid]["keys"] = sorted(
        [k for k in ks if k != key] + [key + "_count"])
    code, out = run(base, mut)
    expect("key renamed -> FAIL, reported as RENAME", FAIL, code, "RENAMED", out)

    # ── 4. ADDITIVE — a new key must NEVER block a PR.
    mut = copy.deepcopy(base)
    mut["endpoints"][eid]["keys"] = sorted(
        mut["endpoints"][eid]["keys"] + ["brand_new_field_xyz"])
    code, out = run(base, mut)
    expect("key added -> PASS (additive never blocked)", PASS, code, "PASS", out)

    # ── 5. ENDPOINT DELETED — every key it served disappeared.
    mut = copy.deepcopy(base)
    del mut["endpoints"][eid]
    code, out = run(base, mut)
    expect("endpoint removed -> FAIL", FAIL, code, "ENDPOINT REMOVED", out)

    # ── 6. VACUOUS PASS #1 — EMPTY surface. The single most important case:
    #       "measured nothing" must never render as "found nothing wrong".
    mut = copy.deepcopy(base)
    mut["endpoints"] = {}
    code, out = run(base, mut)
    expect("EMPTY surface -> UNMEASURED, never PASS", UNMEASURED, code,
           "EMPTY", out)

    # ── 7. VACUOUS PASS #2 — surface collapses below the sanity floor. A
    #       broken extractor must not masquerade as a mass key deletion, and
    #       must not slip through as a pass either.
    mut = copy.deepcopy(base)
    keep = sorted(mut["endpoints"])[: max(1, len(mut["endpoints"]) // 10)]
    mut["endpoints"] = {k: mut["endpoints"][k] for k in keep}
    code, out = run(base, mut)
    expect("surface collapsed below floor -> UNMEASURED", UNMEASURED, code,
           "collapsed", out)

    # ── 8. LAUNDERING — refactor a response into an unresolvable helper.
    #       This must not silently pass (the keys are gone from view) and must
    #       not read as a mass removal either. It is UNMEASURED.
    mut = copy.deepcopy(base)
    mut["endpoints"][eid]["resolution"] = "opaque"
    mut["endpoints"][eid]["keys"] = []
    code, out = run(base, mut)
    expect("resolved -> opaque (laundered) -> UNMEASURED", UNMEASURED, code,
           "opaque", out)

    # ── 9. MISSING BASELINE — cannot compare, so cannot pass.
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = G.check(baseline_path="/nonexistent/surface.json", verbose=True,
                       surface=base)
    expect("missing baseline -> UNMEASURED", UNMEASURED, code, "baseline missing",
           buf.getvalue())

    # ── 10. EXTRACTOR CRASH — an exception must land as UNMEASURED, not as an
    #        unhandled traceback that a shell wrapper could read as anything.
    real = G.extract_surface
    G.extract_surface = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(base, fh)
            p = fh.name
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = G.check(baseline_path=p, verbose=True)
        os.unlink(p)
        expect("extractor raises -> UNMEASURED", UNMEASURED, code, "boom",
               buf.getvalue())
    finally:
        G.extract_surface = real

    # ══ verify-baseline — "is this baseline the surface of THIS tree?" ═════
    # The question a PR that COMMITS a baseline has to answer, asked against
    # main as it is now. check() answers a different one, and answered it
    # "yes" on #5026's merge ref while #5022 was about to remove a key #5026
    # was freezing (see verify_baseline in the guard).

    # ── 11. IDENTITY — the tree's own surface is current.
    code, out = run_verify(base, copy.deepcopy(base))
    expect("verify: identical surface -> CURRENT", PASS, code, "CURRENT", out)

    # ── 12. LINE CHURN — every edit above a handler moves `source`. A refresh
    #        must not read as stale because main.py grew a comment.
    mut = copy.deepcopy(base)
    for rec in mut["endpoints"].values():
        rec["source"] = rec["source"] + "0"
    mut["stats"] = dict(mut["stats"], python_files_scanned=-1)
    code, out = run_verify(base, mut)
    expect("verify: source/stats churn only -> CURRENT", PASS, code, "CURRENT",
           out)

    # ── 13. THE #5026 SHAPE — the baseline names a key the tree does not
    #        serve. Must go red, in the direction that breaks `check` on main.
    mut = copy.deepcopy(base)
    mut["endpoints"][eid]["keys"] = [k for k in mut["endpoints"][eid]["keys"]
                                     if k != key]
    code, out = run_verify(base, mut)
    expect("verify: baseline asserts a key the tree no longer serves -> STALE",
           FAIL, code, "ASSERTED BUT NOT SERVED", out)
    expect("verify: ... and names the key", FAIL, code, key, out)

    # ── 14. GENERATED AGAINST AN OLDER TREE — the tree serves a key the
    #        baseline never recorded. Harmless to `check`, but it is still not
    #        this tree's baseline, and a refresh exists to make it one.
    mut = copy.deepcopy(base)
    mut["endpoints"][eid]["keys"] = sorted(
        mut["endpoints"][eid]["keys"] + ["brand_new_field_xyz"])
    code, out = run_verify(base, mut)
    expect("verify: tree serves a key the baseline lacks -> STALE", FAIL, code,
           "brand_new_field_xyz", out)

    # ── 15. RESOLUTION MOVED — resolved -> opaque makes `check` UNMEASURED on
    #        main; a baseline that still says resolved is stale.
    mut = copy.deepcopy(base)
    mut["endpoints"][eid]["resolution"] = "opaque"
    mut["endpoints"][eid]["keys"] = []
    code, out = run_verify(base, mut)
    expect("verify: resolution changed -> STALE", FAIL, code, "resolution", out)

    # ── 16. VACUOUS CURRENT — an empty surface must never certify a baseline.
    mut = copy.deepcopy(base)
    mut["endpoints"] = {}
    code, out = run_verify(base, mut)
    expect("verify: EMPTY surface -> UNMEASURED, never CURRENT", UNMEASURED,
           code, "EMPTY", out)

    # ── 17. NO ATTRIBUTION — an unattributed surface differs from any real
    #        baseline for reasons unrelated to staleness.
    mut = copy.deepcopy(base)
    mut["url_map"] = dict(mut["url_map"], available=False)
    code, out = run_verify(base, mut)
    expect("verify: url_map unavailable -> UNMEASURED", UNMEASURED, code,
           "url_map", out)

    # ── 18. MISSING BASELINE.
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = G.verify_baseline(baseline_path="/nonexistent/surface.json",
                                 verbose=True, surface=base)
    expect("verify: missing baseline -> UNMEASURED", UNMEASURED, code,
           "baseline missing", buf.getvalue())

    # ── verdict ────────────────────────────────────────────────────────────
    bad = [c for ok, c, _ in _results if not ok]
    print()
    print("═" * 74)
    if bad:
        print(f"SELF-TEST FAILED — {len(bad)}/{len(_results)} case(s) wrong:")
        for c in bad:
            print(f"  - {c}")
        print("The contract guard is NOT trustworthy in this state.")
        print("═" * 74)
        return 1
    print(f"SELF-TEST PASSED — {len(_results)}/{len(_results)} cases. "
          f"The guard goes red on every planted defect,")
    print("passes additive change, and never turns an unmeasurable surface "
          "into a green check.")
    print("═" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
