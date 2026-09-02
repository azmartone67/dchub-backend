"""r-exports-stub (2026-09-02) — the bulk export manifest must not publish a
tier preview under a dataset's full description.

Reproduces the payloads the LIVE manifest was serving on 2026-09-02: 5 of
20,191 facilities and 5 of 300+ markets, each wrapped in an upsell envelope,
gzipped and offered as the dataset. Nothing failed, because nothing looked
inside the body.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routes.r2_exports import (  # noqa: E402
    _as_of,
    _gate_evidence,
    _row_count,
)

# verbatim shapes from the live exports, trimmed to the fields that matter
FACILITIES_STUB = json.dumps({
    "success": True, "count": 5, "data": [{"id": i} for i in range(5)],
    "total_matching": 20191, "full_results_available": True, "tier": "free",
    "upgrade_url": "https://dchub.cloud/pricing", "note": "Free tier: ...",
}).encode()
MARKETS_STUB = json.dumps({
    "count": 5, "data": [{"id": i} for i in range(5)], "total": 317,
    "locked": True, "upsell": "...", "signup_url": "...", "tier": "free",
}).encode()
REAL_NEWS = json.dumps({
    "generated_at": "2026-09-01T09:01:59Z",
    "items": [{"t": i} for i in range(240)],
}).encode()


def test_the_live_facilities_stub_is_caught():
    ev = _gate_evidence(FACILITIES_STUB)
    assert ev is not None
    assert ev["records"] == 5
    assert ev["total_available"] == 20191
    assert ev["truncated"] is True
    assert "upgrade_url" in ev["markers"]


def test_the_live_markets_stub_is_caught():
    ev = _gate_evidence(MARKETS_STUB)
    assert ev is not None
    assert ev["records"] == 5
    assert ev["truncated"] is True
    assert "locked" in ev["markers"]


def test_a_real_dataset_is_not_caught():
    """The guard must not refuse everything — a full payload publishes."""
    assert _gate_evidence(REAL_NEWS) is None


def test_markers_alone_are_enough():
    """An upsell envelope that happens to carry every row is still a preview
    shape; count/total cannot see it."""
    ev = _gate_evidence(json.dumps(
        {"data": [1, 2, 3], "count": 3, "total": 3, "upgrade_url": "x"}).encode())
    assert ev is not None and ev["truncated"] is False


def test_truncation_alone_is_enough():
    """And a trimmed payload whose upsell keys were renamed is caught by the
    count comparison — neither test is load-bearing on its own."""
    ev = _gate_evidence(json.dumps(
        {"data": [1, 2, 3], "count": 3, "total_matching": 900}).encode())
    assert ev is not None
    assert ev["markers"] == [] and ev["truncated"] is True


def test_unparseable_body_is_not_reported_as_a_paywall():
    """An opaque body is a DIFFERENT failure. Calling it a tier gate would
    send someone to fix pricing when the endpoint is returning HTML."""
    assert _gate_evidence(b"<html>502 Bad Gateway</html>") is None
    assert _gate_evidence(b"") is None


def test_row_count_handles_both_shapes():
    assert _row_count([1, 2, 3]) == 3
    assert _row_count({"a": [1, 2], "b": [1, 2, 3, 4]}) == 4
    assert _row_count({"scalar": 1}) is None


def test_as_of_prefers_the_payload_stamp():
    stamp, src = _as_of(REAL_NEWS, "BUILD")
    assert stamp == "2026-09-01T09:01:59Z"
    assert src == "generated_at"


def test_as_of_falls_back_and_says_so():
    stamp, src = _as_of(json.dumps({"items": []}).encode(), "BUILD")
    assert (stamp, src) == ("BUILD", "build_time")
    stamp, src = _as_of(b"not json", "BUILD")
    assert (stamp, src) == (None, "unparseable")
