"""
tests/test_clarity_dead_click_share_math.py — a percentage may not exceed 100
(2026-08-23).

WHAT WENT WRONG. `routes/clarity_insights.py::_hotspots` computed

    dead    = _rows("DeadClickCount", "subTotal")        # a count of CLICKS
    traffic = _rows("Traffic", "totalSessionCount")      # a count of SESSIONS
    share   = round(100.0 * d / t, 1)                    # clicks / sessions

and published the result on a field named `dead_share_pct`. Clicks over
sessions is not a share of anything — it is a per-session rate — so the field
was unbounded. Read live from production on 2026-08-23 before the fix:

    /facilities/fiber-alley-san-diego-...      dead_share_pct  1000.0
    /facilities/iconnect-montana-...           dead_share_pct   800.0
    /facilities/microsoft-azure-south-africa-  dead_share_pct   375.0

Two consequences, both quiet:

  1. Anyone reading the report saw an impossible number, which makes the whole
     detector untrustworthy — and this repo has a standing rule that a
     measurement which cannot be true is worse than no measurement.
  2. `_worth_filing` gated on `dead_share_pct >= 8.0`. With the numerator
     inflated by an order of magnitude that floor passed for essentially every
     URL, so the threshold that was supposed to separate a real hotspot from
     one stray misclick was gating nothing at all.

★ THE SECOND-ORDER TRAP, which is why `strict` exists. `_rows` fell back
through `value_key -> subTotal -> sessionsCount`. So "fixing" this by simply
asking for sessionsCount would, on a payload that does not carry that field,
silently hand back subTotal — the CLICK count again, now wearing a session
name, and the same bug with better vocabulary. Session reads are strict: an
absent field stays absent and the share stays null.

CI-SAFETY: pure payload shaping. No network, no DB, no Clarity quota.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("flask")

from routes.clarity_insights import _hotspots  # noqa: E402


def _payload(dead_rows, traffic_rows, rage_rows=None):
    return [
        {"metricName": "DeadClickCount", "information": dead_rows},
        {"metricName": "RageClickCount", "information": rage_rows or []},
        {"metricName": "Traffic", "information": traffic_rows},
    ]


URL = "https://dchub.cloud/facilities/fiber-alley-san-diego-18d58879"


# ── 1. the exact production regression ───────────────────────────────────

def test_the_1000_percent_row_can_no_longer_happen():
    """The literal shape that produced dead_share_pct=1000.0 in production.

    10 dead clicks over 1 trafficked session, with no session count supplied
    for the numerator. The share must be null — not 1000, and not a guess.
    """
    spots = _hotspots(_payload(
        [{"URL": URL, "subTotal": 10}],
        [{"URL": URL, "totalSessionCount": 1}],
    ))
    assert len(spots) == 1
    s = spots[0]
    assert s["dead_clicks"] == 10, "the click count must survive under its real name"
    assert s["dead_sessions"] is None, (
        "Clarity supplied no sessionsCount; the strict read must leave it None"
        " rather than falling back to the click count")
    assert s["dead_share_pct"] is None, (
        "a share with no session numerator must be null, got %r" % s["dead_share_pct"])
    assert s["dead_clicks_per_session"] == 10.0, (
        "the unbounded rate is where 10-clicks-in-1-session legitimately lives")


@pytest.mark.parametrize("clicks,sessions", [(10, 1), (8, 1), (15, 4), (3, 1)])
def test_share_is_never_above_100_whatever_the_clicks(clicks, sessions):
    """Property: no payload of clicks-per-session may push the PERCENT field
    over 100. This is the invariant the old formula could not hold."""
    spots = _hotspots(_payload(
        [{"URL": URL, "subTotal": clicks}],
        [{"URL": URL, "totalSessionCount": sessions}],
    ))
    share = spots[0]["dead_share_pct"]
    assert share is None or 0.0 <= share <= 100.0, (
        "dead_share_pct=%r for %d clicks / %d sessions" % (share, clicks, sessions))


# ── 2. a real share, when Clarity actually gives us one ──────────────────

def test_real_sessions_based_share_is_computed_when_available():
    """4 of 8 sessions had a dead click -> 50%, regardless of click volume.

    The 40 clicks must NOT enter the percentage; they belong to the rate.
    """
    spots = _hotspots(_payload(
        [{"URL": URL, "subTotal": 40, "sessionsCount": 4}],
        [{"URL": URL, "totalSessionCount": 8}],
    ))
    s = spots[0]
    assert s["dead_sessions"] == 4
    assert s["dead_share_pct"] == 50.0, "got %r" % s["dead_share_pct"]
    assert s["dead_clicks"] == 40
    assert s["dead_clicks_per_session"] == 5.0


# ── 3. the strict-read guard ─────────────────────────────────────────────

def test_absent_session_count_does_not_silently_become_the_click_count():
    """The whole point of strict=True.

    If the lenient fallback ever returns for session reads, dead_sessions
    becomes 999 here — equal to the clicks — and the share is fabricated.
    """
    spots = _hotspots(_payload(
        [{"URL": URL, "subTotal": 999}],
        [{"URL": URL, "totalSessionCount": 3}],
    ))
    s = spots[0]
    assert s["dead_sessions"] is None
    assert s["dead_sessions"] != s["dead_clicks"]
    assert s["dead_share_pct"] is None


# ── 4. controls: the detector still detects ──────────────────────────────

def test_hotspots_still_rank_and_still_carry_rage():
    """Must-stay-green control. If this fails alongside the others, _hotspots
    has simply stopped working rather than started being honest."""
    a = "https://dchub.cloud/a"
    b = "https://dchub.cloud/b"
    spots = _hotspots(_payload(
        [{"URL": a, "subTotal": 3}, {"URL": b, "subTotal": 12}],
        [{"URL": a, "totalSessionCount": 2}, {"URL": b, "totalSessionCount": 6}],
        [{"URL": b, "subTotal": 5}],
    ))
    assert [s["url"] for s in spots] == [b, a], "must sort by dead_clicks desc"
    assert spots[0]["rage_clicks"] == 5
    assert spots[1]["rage_clicks"] == 0


def test_unknown_payload_shape_yields_empty_not_an_exception():
    for junk in (None, {}, "nope", [{"no": "metric"}]):
        assert _hotspots(junk) == []


def test_missing_traffic_row_reports_null_not_zero_division():
    spots = _hotspots(_payload([{"URL": URL, "subTotal": 4}], []))
    s = spots[0]
    assert s["traffic_sessions"] is None
    assert s["dead_share_pct"] is None
    assert s["dead_clicks_per_session"] is None
    assert s["dead_clicks"] == 4


# ── 5. the detector must actually RUN ────────────────────────────────────

def test_dead_click_tick_is_scheduled():
    """A detector nothing calls is not a detector.

    This module shipped 2026-07-10 with a report route, a filer, thresholds
    and a live API token — and no scheduler. It filed zero findings for six
    weeks. That is the registered-but-not-scheduled class
    (tests/test_shell_scheduler_coverage.py exists because of it), and this
    assertion is what stops the dispatch line being dropped again.
    """
    import re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "routes", "cron_heartbeat.py"),
               encoding="utf-8").read()
    assert "/api/v1/admin/clarity/dead-clicks-tick" in src, (
        "the Clarity dead-click tick is not in cron_heartbeat._DISPATCH — it"
        " will never run, exactly as it did not for six weeks")
    # And it must stay at most daily: the Clarity export API allows 10 calls
    # per project per day and a human needs some of them for the read route.
    block = src[src.index("clarity_dead_clicks_daily"):][:400]
    assert re.search(r"now\.hour == \d+", block), (
        "the tick must be pinned to a single hour; a cadence without an hour"
        " guard would burn the 10/day Clarity quota")
