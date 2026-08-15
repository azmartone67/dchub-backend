#!/usr/bin/env python3
"""tests/test_failover_never_serves_a_stale_404.py — a stale failover origin
must never be allowed to tell a crawler a live page is gone.

★ THE DEFECT. Both Render failover branches in worker.js accepted
`renderResp.status < 500`, so a 404 from the failover build was returned to the
client as a real 404. Render runs IS_FAILOVER=true and is a STALE build.
Measured 2026-08-14 against https://dchub-backend-render.onrender.com:

    /press-release/dcpi-v2-launch                     404   (Railway: 200)
    /press-release/2026-07-19-hugging-face-mcp-...    404   (Railway: 200)
    /grid                                             200
    /facilities/directory                             200

So every Railway hiccup told Google and GPTBot that a live page did not exist.

★★ WHY THIS WAS MISDIAGNOSED TWICE BEFORE LANDING HERE. The symptom is an
intermittent 404 that looks like bot management: probing the same 20 URLs four
times returned four different 404 rates (0%, 45%, 50%, 100%). It was blamed on
a Cloudflare challenge page and then on a WAF custom rule. The zone has NO
block rules — all five custom rules are Skip and there are zero rate-limiting
rules. The tell was in the crawler mix at the edge: GPTBot 9.3% 404 AND 2.1%
522. A 522 is origin-reachability, not security. Random-per-request +
UA-independent + correlated with origin pressure is what a FAILOVER looks like.

★★★ 503 SAYS "RETRY". 404 SAYS "DELETE THIS URL". The failover branch is only
reached because the primary already failed, so a secondary's 404 cannot be
distinguished from staleness. The safe reading of "the only origin that
answered is the stale one, and it says no" is a retryable outage. Accepting
2xx/3xx only costs nothing: a 4xx now falls through to KV stale and then 503,
exactly as a Render 5xx already did.

MUST-FAIL — executed, real exit codes, each mutation confirmed applied,
__pycache__ cleared between runs (a same-length digit swap survives the
bytecode cache and reads as a clean restore):

    baseline                          exit=0   4 passed
    M1  site 1 back to < 500          exit=1   2 failed
    M2  site 2 back to < 500          exit=1   2 failed
    M3  both back to < 500            exit=1   2 failed
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(ROOT, "worker.js")


def _src():
    with open(WORKER, encoding="utf-8") as fh:
        s = fh.read()
    assert len(s) > 50_000, f"worker.js suspiciously small ({len(s)}b)"
    return s


def test_every_failover_branch_accepts_2xx_3xx_only():
    """No `renderResp.status < 500` may survive anywhere."""
    src = _src()
    gates = re.findall(r"renderResp\s*&&\s*renderResp\.status\s*<\s*(\d+)", src)
    assert gates, (
        "no Render failover status gate found at all — if the failover was "
        "removed, delete this test in the same commit rather than letting it "
        "pass by finding nothing")
    bad = [g for g in gates if int(g) > 400]
    assert not bad, (
        f"a failover branch still accepts status < {bad[0]} — a stale origin's "
        "404 will be served to crawlers as proof the page is gone. "
        "503 says retry; 404 says delete the URL.")


def test_both_known_branches_are_covered():
    """The HTML path AND the API path. Fixing one leaves the other live."""
    src = _src()
    gates = re.findall(r"renderResp\s*&&\s*renderResp\.status\s*<\s*(\d+)", src)
    assert len(gates) >= 2, (
        f"expected both failover branches (HTML + API), found {len(gates)} — "
        "a branch was removed or renamed and may no longer be guarded")
    assert all(int(g) <= 400 for g in gates)


def test_a_5xx_from_render_still_falls_through():
    """The pre-existing behaviour must be preserved, not just narrowed.

    Tightening the gate is only safe if the fall-through path it lands in is
    still there — otherwise a 4xx now dead-ends instead of reaching KV/503.
    """
    src = _src()
    assert "kvCacheGet" in src, "the KV stale rung disappeared from the ladder"
    assert "Service temporarily unavailable" in src, (
        "the terminal 503 disappeared — a rejected failover response now has "
        "nowhere to fall through to")


def test_version_was_bumped_for_this_worker_edit():
    """worker.js deploys by manual paste; the version header is the only drift
    signal. scripts/check_worker_version_bump.sh enforces the bump on any edit,
    and tests/test_seven_levers_shell.py pins the literal — both must move.
    """
    src = _src()
    m = re.search(r"const WORKER_VERSION = '([^']+)'", src)
    assert m, "WORKER_VERSION const missing"
    assert m.group(1) != "4.9.43-grid-trailing-slash-301", (
        "worker.js changed without a version bump — live and repo would report "
        "the same version with different content")


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
