"""tests/test_facility_claim_guard.py — raw source ROWS published as facilities
(2026-08-17).

THE POST. LinkedIn, 2026-08-17T16:01:31Z, urn:li:share:7495147805028569089:

    "26,000 data-center facilities are now live in DC Hub's index, spanning
     179 countries. That is up from the 18,000+ this c[ycle]..."

Live canon that day, from /api/v1/stats/canonical:

    facilities_distinct = 18,406   <- distinct BUILDINGS, the citeable field
    facilities_records  = 26,137   <- raw source rows, ~1.4x the buildings
    countries_covered   = 178

So the post published the raw pile as buildings, and framed the dedup ratio as
GROWTH over "18,000+" — the number that was already correct.

WHY GUARDS 1-3 PASSED IT. They corroborate a number against canon, and
canonical_stats' `facilities` key IS `COUNT(*) FROM discovered_facilities`. The
claim verified against 26,137 and shipped. Only a check that knows rows are not
buildings can catch this.

The lead template in wins_poster DID carry a qualifier ("— N verified and
deduped —"); the composer re-voices leads and dropped it. So this guard reads
the FINAL text and never trusts upstream labelling.

Run:  python3 -m pytest tests/test_facility_claim_guard.py -v
"""
from __future__ import annotations

import pytest

from routes import media_fact_check_guard as g


DISTINCT, RECORDS = 18406, 26137


@pytest.fixture
def live(monkeypatch):
    """Pin the live canon to the 2026-08-17 reading."""
    monkeypatch.setattr(g, "_live_facility_counts", lambda: (DISTINCT, RECORDS))


# ── THE PIN ────────────────────────────────────────────────────────────────

def test_the_published_post_is_blocked(live):
    """THE PIN — verbatim from the post that shipped."""
    text = ("26,000 data-center facilities are now live in DC Hub's index, "
            "spanning 179 countries. That is up from the 18,000+ this cycle.")
    out = g.check_facility_count_claims(text)
    assert out["over"], "the raw row count published as buildings must not pass"
    assert out["over"][0]["value"] == 26000
    assert out["live_distinct"] == DISTINCT


def test_the_citeable_number_passes(live):
    """Inverse control: the honest version of the same sentence must publish,
    or the guard is just a mute button."""
    out = g.check_facility_count_claims(
        "18,300+ data-center facilities are now live in DC Hub's index.")
    assert out["over"] == []
    assert out["claims"], "the claim must still be EXTRACTED, just not flagged"


def test_raw_count_is_allowed_when_named_as_records(live):
    """The raw pile is real and citeable — as records. This is the escape
    hatch, and it must actually work."""
    out = g.check_facility_count_claims(
        "26,000 facilities in the index, deduped from raw source records.")
    assert out["over"] == []


def test_a_qualifier_does_not_license_any_number(live):
    """'source records' raises the ceiling to the row count, not to infinity."""
    out = g.check_facility_count_claims(
        "90,000 facilities tracked as raw source records.")
    assert out["over"], "a qualifier must not wave through a number above the rows"


# ── extraction shapes ──────────────────────────────────────────────────────

@pytest.mark.parametrize("phrase", [
    "26,000 facilities",
    "26,000 data-center facilities",
    "26,000 data center facilities",
    "26,000 data centers",
    "26,000+ facilities",
    "26,000 tracked facilities",
    "26,000 distinct facilities",
])
def test_claim_shapes_are_all_caught(live, phrase):
    """One phrasing slipping the regex reopens the whole hole."""
    assert g.check_facility_count_claims(f"DC Hub now indexes {phrase}.")["over"], \
        f"missed: {phrase}"


def test_unrelated_numbers_are_not_facility_claims(live):
    """False positives would block honest posts — the more likely way a guard
    gets switched off."""
    out = g.check_facility_count_claims(
        "94,626 transmission lines and 127,196 substations across 178 countries.")
    assert out["claims"] == []
    assert out["over"] == []


def test_small_numbers_are_not_swallowed(live):
    """A three-digit floor keeps '5 facilities in Ashburn' out of the canon
    comparison, where it would be meaningless."""
    assert g.check_facility_count_claims("5 facilities in Ashburn")["claims"] == []


# ── fail-closed ────────────────────────────────────────────────────────────

def test_unreadable_canon_fails_closed(monkeypatch):
    """An unprovable building count must never publish. This is the posture
    check_agent_count_claims already takes."""
    monkeypatch.setattr(g, "_live_facility_counts", lambda: (None, None))
    out = g.check_facility_count_claims("26,000 data-center facilities.")
    assert out["live_distinct"] is None
    assert len(out["over"]) == 1, "claims must be treated as over when live is unknown"


def test_no_claims_means_nothing_to_fail(monkeypatch):
    """Fail-closed must not mean fail-always: a text with no facility claim is
    unaffected even when the DB is down."""
    monkeypatch.setattr(g, "_live_facility_counts", lambda: (None, None))
    out = g.check_facility_count_claims("DC Hub tracks live grid telemetry.")
    assert out["claims"] == [] and out["over"] == []


def test_tolerance_absorbs_rounding_not_the_dedup_ratio(live):
    """Copy rounds; a 1.4x basis swap is not rounding."""
    assert g.check_facility_count_claims("18,500 facilities")["over"] == []
    assert g.check_facility_count_claims("26,000 facilities")["over"]


def test_extractor_never_raises_on_junk():
    assert g._extract_facility_count_claims(None) == []
    assert g._extract_facility_count_claims("") == []
