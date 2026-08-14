#!/usr/bin/env python3
"""tests/test_grid_trailing_slash_301.py — /grid/ must not be a 404 dead end,
and a paid ISO must not answer on two spellings.

★ WHY THIS DEFECT SURVIVED A FIX THAT TARGETED IT. dchub-frontend #1180 added a
section-root trailing-slash normaliser and listed 'grid' in its allowlist. It
shipped, deployed, and /grid/ stayed 404 — because the zone route
`dchub.cloud/grid/*` sends these paths to THIS worker before Cloudflare Pages
is ever consulted. A Pages deploy cannot reach them. The frontend guard passed
the whole time: it executed the block in isolation, where the block is correct.
Registration is not execution, and a unit test of an unreachable branch is a
vacuous pass.

MEASURED LIVE 2026-08-14, before this change:

    /grid          200
    /grid/         404   <- dead end, one character from a live page
    /grid/pjm/     301 -> /grid/pjm    free ISO: falls through to Pages,
    /grid/ercot/   301 -> /grid/ercot  which normalises it
    /grid/miso/    200   <- the SAME PAGE as /grid/miso, both 200, and
    /grid/spp/     200      NEITHER carries a rel=canonical
    /grid/caiso/   200

The paid-ISO proxy in worker.js exists for a good reason (tier-varying HTML
must not be edge-cached by path), but it also meant paid ISOs never reached the
normaliser that free ISOs got for free. The result was "Duplicate without
user-selected canonical", by construction, on exactly the pages we charge for.

★★ THIS TEST USES THE SHIPPED REGEX, NOT A COPY. It extracts the pattern
literal out of worker.js and runs the real thing. A reimplementation here would
pass while the worker did something else — which is precisely how the frontend
guard stayed green against a 404.

★★★ IT ALSO PINS ORDERING. The normaliser must appear BEFORE the paid-ISO
proxy block. Placed after, it would still pass a naive behaviour test while
never running for /grid/miso/ — the same unreachable-branch failure this file
was written in response to.

MUST-FAIL — EXECUTED under pytest, real exit codes, each mutation CONFIRMED
APPLIED before running (a mutation that silently fails to apply reports the
unmutated file as proof):

    baseline                            exit=0   3 passed
    M1  slug capture group removed      exit=1   1 failed
    M2  status: 301 -> 200 (rewrite)    exit=1   1 failed
    M3  x-dc-hub-source marker renamed  exit=1   2 failed
    M4  /i flag dropped                 exit=1   1 failed  (2 checks red)
    M5  normaliser moved BELOW the      exit=1   1 failed
        paid-ISO proxy

★ M2 AND M3 WERE GREEN ON THE FIRST ATTEMPT, AND THAT IS WHY _Guard EXISTS —
see the class docstring below. The suite reported PASSED with the rule turned
into a rewrite. Do not trust a mutation run that was not confirmed applied,
and do not trust a check() helper that only collects.

★★ M5 WAS ALSO GREEN ONCE, FOR A DIFFERENT REASON. Bumping WORKER_VERSION to
'4.9.43-grid-trailing-slash-301' made a bare find("grid-trailing-slash-301")
match the version CONST near line 433 instead of the rule ~2900 lines below —
pointing the status-code window at the wrong region and making the ordering
check pass vacuously, since the const naturally precedes the proxy. Both now
anchor on _MARK, the full header assignment, which appears exactly once. A
marker that also matches your own version string is not a marker.

Run standalone:   python3 tests/test_grid_trailing_slash_301.py
Run under pytest: pytest tests/test_grid_trailing_slash_301.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(ROOT, "worker.js")

# ★ ANCHOR ON THE HEADER ASSIGNMENT, NOT THE BARE STRING. WORKER_VERSION is
# now '4.9.43-grid-trailing-slash-301', so a bare find("grid-trailing-slash-301")
# matches the version const near line 433 instead of the rule ~2900 lines
# below. That would silently point the status-code window at the wrong region
# AND make the ordering check pass vacuously (the const precedes the proxy).
# Caught when the version bump landed: the redirect test went red and the
# ordering test stayed green for the wrong reason.
_MARK = "'x-dc-hub-source': 'grid-trailing-slash-301'"

_failed = []


def check(name, ok, detail=""):
    if ok:
        print(f"  ok   {name}")
    else:
        _failed.append(name)
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


class _Guard:
    """Fail the enclosing pytest test if any check() inside it failed.

    ★ THE FIRST CUT OF THIS FILE HAD NO SUCH THING, AND IT WAS A SILENT GREEN.
    check() appended to a list and printed; nothing raised. Standalone
    (__main__) inspected the list and exited 1, so it looked fine — but under
    pytest each test function simply returned, and pytest reported PASSED no
    matter how many checks had failed. Caught by mutation testing: flipping
    `status: 301` to `status: 200` left the suite green, and the one mutation
    that DID go red only did so by raising IndexError on a missing capture
    group — an accident, not the assertion doing its job.

    A collector without an assertion is a reporting tool, not a guard.
    """

    def __enter__(self):
        self._at = len(_failed)
        return self

    def __exit__(self, *exc):
        if exc[0] is not None:
            return False
        new = _failed[self._at:]
        assert not new, f"{len(new)} check(s) failed: " + "; ".join(new)
        return False


def _src():
    assert os.path.exists(WORKER), "worker.js missing"
    with open(WORKER, encoding="utf-8") as fh:
        s = fh.read()
    # An empty/truncated read makes every search below vacuously "not found",
    # which would read as a clean pass on a broken extraction.
    assert len(s) > 50_000, f"worker.js suspiciously small ({len(s)}b)"
    return s


def test_grid_trailing_slash_is_normalised():
    with _Guard():
        src = _src()

        # ---- extract the SHIPPED pattern -------------------------------------
        m = re.search(r"const\s+_gs\s*=\s*pathname\.match\(/(.+?)/([a-z]*)\)", src)
        check("normaliser is present in worker.js", m is not None,
              "the /grid trailing-slash rule is gone — if removed on purpose, "
              "delete this test in the same commit rather than letting it find nothing")
        if not m:
            return

        js_pat, js_flags = m.group(1), m.group(2)
        # JS -> Python: only difference in this pattern class is the escaped slash.
        py_pat = js_pat.replace(r"\/", "/")
        rx = re.compile(py_pat, re.IGNORECASE if "i" in js_flags else 0)
        check("pattern is case-insensitive", "i" in js_flags,
              "/GRID/ would not normalise")

        def dest(path):
            g = rx.match(path)
            if not g:
                return "untouched"
            slug = g.group(1)
            return f"/grid/{slug}" if slug else "/grid"

        print("\n  the 404 dead end and the duplicate spellings:")
        for path, want in [
            ("/grid/", "/grid"),
            ("/grid/miso/", "/grid/miso"),
            ("/grid/spp/", "/grid/spp"),
            ("/grid/caiso/", "/grid/caiso"),
            ("/grid/pjm/", "/grid/pjm"),
            ("/grid/ercot/", "/grid/ercot"),
            ("/GRID/MISO/", "/grid/MISO"),
        ]:
            got = dest(path)
            check(f"{path} -> 301 {want}", got == want, f"got {got}")

        print("\n  MUST be left alone (no trailing slash = already canonical):")
        for path in ["/grid", "/grid/miso", "/grid/pjm", "/grid/ercot",
                     "/gridiron", "/grid-intelligence", "/api/v1/grid/demand"]:
            got = dest(path)
            check(f"{path} untouched", got == "untouched", f"got {got}")


def test_normaliser_runs_before_the_paid_iso_proxy():
    """Ordering is the whole fix for paid ISOs.

    The proxy below sends /grid/<paid-iso> straight to Railway with no edge
    cache. Anything placed after it never sees those paths, so a normaliser
    that sits below would fix /grid/ and leave /grid/miso/ answering 200 —
    passing a behaviour test while changing nothing that mattered.
    """
    with _Guard():
        src = _src()
        norm = src.find(_MARK)
        proxy = src.find("worker-grid-nocache")
        check("both blocks found", norm > 0 and proxy > 0,
              f"normaliser at {norm}, paid-ISO proxy at {proxy}")
        if norm > 0 and proxy > 0:
            check("normaliser precedes the paid-ISO proxy", norm < proxy,
                  "the normaliser sits AFTER the proxy — /grid/<paid-iso>/ never "
                  "reaches it, so paid ISOs keep answering on two URLs")


def test_it_redirects_rather_than_rewrites():
    """A 301, matching what Pages already emits for /grid/pjm/.

    Two spellings of the same normalisation answering with different status
    codes is its own drift, and a rewrite (200 at both URLs) would leave the
    duplicate exactly as it was.
    """
    with _Guard():
        src = _src()
        i = src.find(_MARK)
        if i < 0:
            check("normaliser present", False, "not found")
            return
        window = src[max(0, i - 900):i + 200]
        check("emits status 301", "status: 301" in window,
              "not a 301 — a rewrite would serve 200 at both URLs and keep the duplicate")
        check("sets a Location header", "'Location'" in window, "no Location header")


if __name__ == "__main__":
    test_grid_trailing_slash_is_normalised()
    print()
    test_normaliser_runs_before_the_paid_iso_proxy()
    print()
    test_it_redirects_rather_than_rewrites()
    print("\nFAILED: " + ", ".join(_failed) if _failed else "\nPASSED")
    sys.exit(1 if _failed else 0)
