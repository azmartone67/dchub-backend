"""Novelty is a SUBSTANCE signal, not a scheduling one — 2026-07-28.

_quality_score's novelty term used to read `_classify_post_for_dedup(text) !=
"other"`. That function answers "which daily rate-limit bucket is this post
in?" — a SCHEDULING question. Its buckets are campaign types ("switzerland
model", "tony bishop", "open invitation") that say nothing about substance, and
it reads only the first 300 chars because a bucket is about a post's LEAD.

Using it as the novelty signal was wrong in both directions:
  • a post naming ERCOT, PJM and three countries scored ZERO novelty for
    fitting no rate-limit bucket (the measured case: the pillars X card, three
    real figures, 0.350 and refused);
  • boilerplate scored full novelty for fitting one.

It also coupled two unrelated knobs: adding a rate-limit bucket silently raised
scores, and making a post more specific silently moved which daily cap it
competed under.

_names_concrete_subject asks the substance question directly. Its stop-set is
units + generic abbreviations + DC Hub's own name — small and stable. It is NOT
a topic taxonomy: no new market, operator or protocol ever has to be added for
this to recognise it, which is the failure mode _METRIC_PATTERNS has hit five
times.

Pure functions only; never imports main.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

cp = pytest.importorskip("content_publisher")  # noqa: E402

LINK = " https://dchub.cloud/dcpi"


# ── naming a real subject is what counts ───────────────────────────────────

@pytest.mark.parametrize("text", [
    "7 of 7 US ISOs report live demand — PJM, ERCOT, MISO and CAISO.",
    "Con Edison and Central Hudson publish what their feeders can take.",
    "Japan, South Korea and Brazil now rank beside the US zones.",
    "Ashburn stayed flat while Cheyenne moved this week.",
    "Tony Bishop on why the grid is the constraint.",
    "Point your MCP server at DC Hub and call score_facility.",
    "The Switzerland model: an open invitation to every brokerage.",
])
def test_named_subjects_are_specific(text):
    assert cp._names_concrete_subject(text) is True


# ── talking only about yourself, in generalities, is not ───────────────────

@pytest.mark.parametrize("text", [
    "100% free access to DC Hub, sign up today.",
    "DC Hub is the fastest way to get data. Try it now.",
    "The new platform is here. This is a big step for every team.",
    "Up to 250 MW and 4 GW of capacity, today.",     # units are vocabulary
    "Three things shipped this month that a model cannot fake.",
    "",
])
def test_self_reference_and_generics_are_not_specific(text):
    assert cp._names_concrete_subject(text) is False


def test_sentence_openers_are_not_mistaken_for_names():
    """'Three'/'Where'/'Most' are capitalised by grammar, not by being names.
    Position, not a word list, is what rules them out."""
    for text in ("Three things shipped.", "Where a utility publishes thin data.",
                 "Most market data is a quarterly PDF."):
        assert cp._names_concrete_subject(text) is False
    # ...but the same word mid-sentence next to a real name still works
    assert cp._names_concrete_subject("Most of ERCOT reports hourly.") is True


def test_specificity_never_raises():
    for bad in (None, 12345, object(), "•••", "A" * 5000, "\n\n\n"):
        assert cp._names_concrete_subject(bad) in (True, False)


# ── the decoupling itself ──────────────────────────────────────────────────

def test_novelty_no_longer_depends_on_the_dedup_class():
    """A post in NO rate-limit bucket that names real operators must still earn
    novelty — this is the exact case the old signal punished."""
    text = ("7 of 7 US ISOs report live demand — PJM, ERCOT, MISO, CAISO, SPP, "
            "NYISO and ISO-NE — right now." + LINK)
    assert cp._classify_post_for_dedup(text) == "other"
    assert cp._names_concrete_subject(text) is True
    assert cp._quality_score(text) >= cp.QUALITY_MIN


def test_dedup_classifier_still_does_its_own_job():
    """It must keep classifying for the per-class daily cap; only its use as a
    QUALITY signal was removed."""
    assert cp._classify_post_for_dedup(
        "📍 Cheyenne · WECC · DCPI verdict: BUILD") == "dcpi_verdict"
    assert cp._classify_post_for_dedup(
        "Point your MCP server at DC Hub") == "mcp_pitch"
    assert cp._classify_post_for_dedup("Nothing in particular here") == "other"


@pytest.mark.parametrize("text", [
    "📍 Cheyenne · WECC · DCPI verdict: BUILD at 69.5 today." + LINK,
    "The Switzerland model: an open invitation to every brokerage. Write to "
    "partnerships@dchub.cloud this week." + LINK,
    "Point your MCP server at DC Hub and let an AI agent call score_facility "
    "directly this week." + LINK,
    "Tony Bishop on why the grid, not the chip, is the constraint." + LINK,
    "We added 41 markets in the last 7 days." + LINK,
])
def test_posts_that_used_to_earn_novelty_by_class_still_do(text):
    """Every dedup class that previously got novelty from its BUCKET must now
    get it from naming a subject or from its metric signature — otherwise this
    change would silently block a whole class of posts."""
    sig = cp._post_headline_signature(text)
    assert (cp._names_concrete_subject(text)
            or sig.get("market_verdict") or sig.get("metric_label")), (
        "this post lost its only novelty source: %r" % text[:60])


# ── the drafts that motivated it ───────────────────────────────────────────

def test_pillars_x_is_finally_publishable():
    psm = pytest.importorskip("routes.pillars_master_shell")
    x = psm._drafts({
        "dc_verified": 4923, "dc_tracked": 12650, "countries": 170,
        "markets": 311, "ranked_count": 24,
        "continents_ranked": ["NA", "EU", "AS", "SA", "OC"],
    })["x"]
    assert cp._names_concrete_subject(x) is True
    score = cp._quality_score(x)
    assert score >= cp.QUALITY_MIN, "pillars X scored %.3f" % score


# ── the floors that must not move ──────────────────────────────────────────

def test_hard_floors_survive():
    assert cp._quality_score("Deal - Google") <= 0.15
    assert cp._post_headline_signature(
        "DC Hub MCP served 0 AI tool calls in the last 24h.")["zero_stat"] is True
    assert cp._quality_score("") == 0.0
