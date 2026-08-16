"""tests/test_publisher_silent_slot_loss.py — the publisher's THIRD silent failure (2026-08-16).

Two defects, one root cause: a slot that produces nothing must SAY so, and a slot
whose hour has passed must not be dumped hours late.

★ ABANDONED CLAIMS. `_claim_slot` pre-inserts
`success=FALSE, error_msg='claimed_in_flight'` and sets ONLY `claimed_at`;
`_record` fills `posted_at` later. Die in between and the row is stranded — and
it reads like "in progress", never like "failed". Worse, EVERY counter feeding
the verdict filtered on `posted_at`, which a stranded row never gets, so the
rows were invisible even to `attempts_7d`.

Measured 08-08..08-15: 10 of 30 slots ended `claimed_in_flight`, none with a
success row for the same (slot_date, slot_hour) — they produced nothing. With 8
stranded claims in-window the publisher still reported **healthy**, because
`MEDIA_LINKEDIN_WEEKLY_FLOOR=7` sat against a 4-slots/day = **28/wk** cadence:
lose 75% of the feed, still pass.

★ THE BURST. The catch-up backfill had no age bound. Once the publish block
cleared on 08-15, four consecutive ticks each took the next-most-recent due slot
and fired 20:48 / 20:49 / 20:54 / 21:01 — four posts to one company page in 13
minutes, three carrying the same headline.

Run:  python3 -m pytest tests/test_publisher_silent_slot_loss.py -v
"""
from __future__ import annotations

import pytest

from routes.dchub_media_revival import linkedin_publisher_verdict
from routes import linkedin_quad_daily as lq


# A publisher that is otherwise perfectly healthy: publishing at cadence, on
# time, with cards, nothing gate-blocked. Only the field under test varies.
def _healthy(**over):
    q = {
        "hours_since_success": 2.0,
        "published_24h": 4,
        "published_7d": 28,
        "attempts_7d": 28,
        "with_image_7d": 28,
        "gate_blocked_3d": 0,
        "abandoned_claims_7d": 0,
    }
    q.update(over)
    return q


# ── abandoned claims must be counted, surfaced, and must move the verdict ──

def test_healthy_baseline_is_healthy():
    """Control: without this, every assertion below could pass vacuously."""
    assert linkedin_publisher_verdict(_healthy())["verdict"] == "healthy"


def test_abandoned_claims_are_surfaced():
    v = linkedin_publisher_verdict(_healthy(abandoned_claims_7d=8))
    assert v["abandoned_claims_7d"] == 8


def test_stranded_claims_degrade_the_verdict():
    """THE PIN: 8 slots that produced nothing must not read 'healthy'."""
    v = linkedin_publisher_verdict(_healthy(abandoned_claims_7d=8))
    assert v["verdict"] == "degraded"
    assert any("claimed then abandoned" in r for r in v["reasons"])


def test_a_couple_of_stranded_claims_is_tolerated():
    """Below the threshold this is noise, not an outage — no false alarm."""
    assert linkedin_publisher_verdict(_healthy(abandoned_claims_7d=2))["verdict"] == "healthy"


def test_missing_key_does_not_crash_or_alarm():
    """A pre-migration snapshot has no such key; absence must read as 0."""
    q = _healthy()
    del q["abandoned_claims_7d"]
    v = linkedin_publisher_verdict(q)
    assert v["verdict"] == "healthy"
    assert v["abandoned_claims_7d"] == 0


# ── the cadence floor must reflect the real 28/wk cadence ──────────────────

def test_quarter_strength_feed_is_not_healthy():
    """13/wk against a 4-slots/day cadence is a broken feed, not a healthy one.

    This is the exact reading the old floor of 7 passed.
    """
    v = linkedin_publisher_verdict(_healthy(published_7d=13, with_image_7d=13))
    assert v["verdict"] == "weak"


def test_full_cadence_still_healthy():
    """The raised floor must not condemn a publisher that is doing its job."""
    assert linkedin_publisher_verdict(_healthy(published_7d=28))["verdict"] == "healthy"


# ── the catch-up must skip stale slots ─────────────────────────────────────

SLOTS = [{"hour": h, "topic": f"t{h}", "style": "s"} for h in (8, 12, 16, 20)]
NONE_POSTED = lambda h: False


def test_catchup_takes_a_slot_inside_the_window():
    """A genuinely recent miss is still backfilled — the feature still works."""
    got = lq._catchup_slot(SLOTS, 21, NONE_POSTED)
    assert got is not None and got["hour"] == 20


def test_catchup_refuses_the_whole_stale_day():
    """THE BURST PIN: at 21:00 with nothing posted, only the 20:00 slot is
    eligible. 08/12/16 are missed, not pending."""
    got = lq._catchup_slot(SLOTS, 21, NONE_POSTED)
    assert got["hour"] == 20
    # and once 20 is posted, the day is over — no cascade down to 16/12/8
    assert lq._catchup_slot(SLOTS, 21, lambda h: h == 20) is None


def test_catchup_does_not_fire_0800_at_2100():
    """The literal 08-15 event: 08:00 must never be published at 21:01."""
    got = lq._catchup_slot(SLOTS, 21, lambda h: h in (12, 16, 20))
    assert got is None, "the 08:00 slot is 13h stale and must be abandoned"


def test_catchup_never_returns_a_future_slot():
    got = lq._catchup_slot(SLOTS, 9, NONE_POSTED)
    assert got["hour"] == 8


def test_catchup_window_is_configurable(monkeypatch):
    monkeypatch.setenv("LINKEDIN_QUAD_CATCHUP_MAX_AGE_HOURS", "13")
    got = lq._catchup_slot(SLOTS, 21, lambda h: h in (12, 16, 20))
    assert got is not None and got["hour"] == 8, "an explicit wide window must still work"
