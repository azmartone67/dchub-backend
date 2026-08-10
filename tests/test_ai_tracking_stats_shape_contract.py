"""/api/v1/ai-tracking/stats — the top-level shape contract.

★ WHY THIS EXISTS (2026-08-10). This route is the FALLBACK arm of /ai's
`fetchTrackingData`; the page reaches it whenever /api/ai/tracking is slow or
503s (measured live 2026-08-10: p50 2.3s, 1-in-20 responses 503, 2-in-20 over
the frontend's abort budget). Every field the page needs lived ONLY under the
`stats` wrapper, so `data.platforms_active`, `data.total_requests_all_time` and
`data.total_requests_today` each evaluated to undefined in the browser. The page
turned those undefineds into a confident "0 AI PLATFORMS CONNECTED",
"0 active / 14 tracked" and three em-dash tiles — while THIS payload held
16 platforms and 312,928 requests. Contract-audit mismatch #2.

These tests pin the properties that make that unrecreatable:

  1. The nested `stats` block keeps its canonical names. The fix is ADDITIVE;
     dropping `stats` would just move the breakage onto today's readers.
  2. Every top-level alias exists AND equals its nested counterpart. An alias
     that drifts from its source is worse than no alias at all.
  3. There is NO `total_requests_today` and NO `platforms` key, and the payload
     positively declares has_platform_census=False. This route measures neither
     a today window nor a per-platform census; emitting 0 for a figure nobody
     measured is the flattering-zero defect, and a 0 is worse than a dash
     because it is quotable.
  4. The honest totals still exclude the transport/probe buckets — the aliases
     must not quietly re-import the ~59% infrastructure inflation that r71
     removed.

Tested against the pure builder rather than the Flask route on purpose:
tests/conftest.py deliberately never imports main (no app, no DB, no network),
and a test that could only run against a live database would be skipped in CI —
which is how a guard silently stops guarding.
"""
import pytest

from ai_tracking_stats_shape import build_ai_tracking_stats_payload

# (platform, total_requests, requests_7d, last_seen)
ROWS = [
    ("claude",   124883, 7667, "2026-08-09 22:56:15+00"),
    ("chatgpt",   47501,  349, "2026-08-10 02:28:19+00"),
    ("gemini",    32700,  120, "2026-08-09 10:00:00+00"),
    ("zerohits",      0,    0, "2026-08-01 00:00:00+00"),  # real, but no traffic
    ("mcp",      900000, 5000, "2026-08-10 03:00:00+00"),  # transport bucket
    ("internal", 400000, 2000, "2026-08-10 03:00:00+00"),  # self-traffic
]

REAL = {"claude", "chatgpt", "gemini", "zerohits"}


def _is_real(p):
    return (p or "").strip().lower() in REAL


EXPECTED_PLATFORMS = 3            # zerohits has 0 requests -> not "active"
EXPECTED_TOTAL = 124883 + 47501 + 32700
EXPECTED_7D = 7667 + 349 + 120

ALIASES = {
    "platforms_active": "total_platforms",
    "total_platforms": "total_platforms",
    "total_requests_all_time": "total_requests",
    "requests_7d": "requests_7d",
    "last_activity": "last_activity",
}


@pytest.fixture()
def payload():
    p = build_ai_tracking_stats_payload(ROWS, _is_real)
    # Anti-vacuity: if the builder ever returns something degenerate, every
    # assertion below would be inspecting an empty dict and passing for free.
    assert p.get("success") is True
    assert p.get("stats"), "no `stats` block — nothing meaningful to assert on"
    return p


def test_nested_stats_block_survives(payload):
    """The fix is ADDITIVE. Today's readers of `stats` must keep working."""
    stats = payload["stats"]
    for key in ("total_platforms", "total_requests", "requests_7d",
                "total_requests_including_infrastructure",
                "requests_7d_including_infrastructure",
                "total_requests_label", "last_activity"):
        assert key in stats, f"stats.{key} disappeared — this fix must not remove it"
    assert stats["total_platforms"] == EXPECTED_PLATFORMS
    assert stats["total_requests"] == EXPECTED_TOTAL


def test_top_level_aliases_exist(payload):
    """Undefined at the top level is what the page rendered as a confident 0."""
    for alias in ALIASES:
        assert alias in payload, (
            f"top-level `{alias}` missing — a reader of it gets undefined, which "
            f"/ai rendered as 0 on 2026-08-09"
        )


def test_top_level_aliases_match_nested(payload):
    """An alias that drifts from its source is worse than no alias."""
    stats = payload["stats"]
    for alias, nested in ALIASES.items():
        assert payload[alias] == stats[nested], (
            f"alias `{alias}`={payload[alias]!r} drifted from "
            f"stats.{nested}={stats[nested]!r}"
        )


def test_aliases_carry_real_values_not_zero(payload):
    """A zero-valued alias would reproduce the bug while looking fixed."""
    assert payload["platforms_active"] == EXPECTED_PLATFORMS
    assert payload["total_requests_all_time"] == EXPECTED_TOTAL
    assert payload["requests_7d"] == EXPECTED_7D


def test_honest_totals_still_exclude_infrastructure(payload):
    """The aliases must not re-import the transport/probe inflation r71 removed."""
    stats = payload["stats"]
    assert payload["total_requests_all_time"] == EXPECTED_TOTAL
    assert stats["total_requests_including_infrastructure"] == sum(r[1] for r in ROWS)
    assert stats["total_requests_including_infrastructure"] > payload["total_requests_all_time"]


def test_no_fabricated_today_and_no_fake_census(payload):
    """This route measures neither. Absent is honest; 0 is a lie."""
    assert "total_requests_today" not in payload, (
        "this route has no today window — emitting one (even 0) forges a "
        "measurement and is the exact defect being fixed"
    )
    assert "platforms" not in payload, (
        "this route has no per-platform census; an empty `platforms` object is "
        "counted by the /ai grid as '0 active'"
    )
    assert payload.get("has_platform_census") is False, (
        "the payload must DECLARE that it cannot answer the census question, so "
        "a reader renders unknown instead of zero"
    )


def test_empty_input_reports_zero_not_none(payload):
    """A genuinely empty table is a MEASURED zero — that one is allowed to be 0."""
    empty = build_ai_tracking_stats_payload([], _is_real)
    assert empty["platforms_active"] == 0
    assert empty["total_requests_all_time"] == 0
    assert empty["last_activity"] is None
    # ...but it still must not invent a today figure or a census.
    assert "total_requests_today" not in empty
    assert empty["has_platform_census"] is False
