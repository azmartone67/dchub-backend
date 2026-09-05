"""A listing the loop cannot READ must never be recorded as clean.

★ THE DEFECT. run_white_glove_propagation did `body, status = _polite_get(url)`
and then never read `status`. A registry behind a bot wall answers with a real
body — cursor.directory returns HTTP 429 with a ~29KB "Vercel Security
Checkpoint" page — so `body` was truthy, the fetch_failed branch was skipped,
and the challenge page went to detect_number_drift. A challenge page contains no
numbers, so the detector correctly returned [], and the registry was filed under
summary["clean"].

The detector was never wrong. Its zero-false-positive contract was being
satisfied by a page that is not the listing. "No drift found" and "I never saw
the listing" produced the identical outcome, and only one of them is good news.

★ MEASURED 2026-09-05 by probing all 15 SEED_REGISTRIES hosts exactly as the
loop does — five unreadable, six counting the empty 200:

    cursor.directory   HTTP 429 + Vercel checkpoint (29,405 bytes)
    pulsemcp.com       HTTP 403 + challenge
    hub.continue.dev   connection error
    dxt.so             connection error
    www.klavis.ai      HTTP 404
    smith.land         HTTP 200 with a ZERO-BYTE body

Every one had been reporting "clean" for as long as the lane had run. The
cursor.directory drift that exposed it (33 tools against a canon of 83) is
simply the one a human happened to open.

★ THE 200-WITH-EMPTY-BODY CASE is why the gate cannot be a status-code check.
smith.land answers 200 with nothing; any status-only rule calls that a clean
listing.
"""
import pytest

from routes.white_glove_propagation import (
    MIN_LISTING_BYTES,
    classify_reachability,
    detect_number_drift,
)

_REAL_LISTING = (
    "DC Hub MCP Server. Live data layer for data-center intelligence. "
    "33 MCP tools cover 21,433 facilities across 170+ countries, "
    "DCPI (232 markets scored daily), 2,000+ M&A deals. dchub.cloud"
)
_VERCEL = ("<html><head><title>Vercel Security Checkpoint</title></head>"
           "<body>Verifying your browser before accessing cursor.directory."
           + "x" * 2000 + "</body></html>")


# ── the six measured failure shapes ──────────────────────────────────────
@pytest.mark.parametrize("body,status,expect_reason", [
    (_VERCEL, 429, "HTTP 429 + bot challenge"),   # cursor.directory
    ("<html>Attention Required" + "x" * 2000, 403, "HTTP 403 + bot challenge"),
    (None, None, "no response"),                  # hub.continue.dev / dxt.so
    ("<html>not found</html>" + "x" * 2000, 404, "HTTP 404"),   # klavis
    ("", 200, "empty body"),                      # smith.land ★
    ("   ", 200, "empty body"),
])
def test_unreadable_pages_are_named_not_swallowed(body, status, expect_reason):
    assert classify_reachability(body, status) == expect_reason


def test_a_challenge_served_at_200_is_still_unreachable():
    """The same blindness without the status-code tell."""
    assert classify_reachability(_VERCEL, 200) == "bot challenge page"


def test_a_tiny_200_body_is_not_a_listing():
    assert classify_reachability("hi", 200).startswith("body too small")


def test_a_real_listing_passes_the_gate(): 
    assert classify_reachability(_REAL_LISTING + "x" * 2000, 200) is None


def test_gate_does_not_depend_on_status_being_present():
    """_polite_get may return status=None; a good body must still pass."""
    assert classify_reachability(_REAL_LISTING + "x" * 2000, None) is None


# ── the property that actually failed in production ──────────────────────
def test_the_checkpoint_page_yields_no_drift_which_is_why_the_gate_is_needed():
    """This is the whole bug in one assertion.

    The detector is CORRECT to find nothing here. That is precisely why the
    gate must run first — an empty finding list from an unreadable page is
    indistinguishable from an empty list from a clean one.
    """
    canon = {"tools": 83, "tools_live": 83}
    assert detect_number_drift(_VERCEL, canon, scope_to_dchub=False) == []
    # ...while the REAL page the loop never sees is plainly drifted
    found = detect_number_drift(_REAL_LISTING, canon, scope_to_dchub=False)
    assert any(d["kind"] == "tools" and d["found"] == 33 for d in found), found


def test_min_listing_bytes_is_below_the_smallest_real_listing():
    """Guards the threshold itself: the smallest genuine listing measured
    (mcphive) was 11,189 bytes, so 500 has ~22x headroom. If someone raises
    this above a real listing size, real pages start reporting unreachable."""
    assert 0 < MIN_LISTING_BYTES < 11189
