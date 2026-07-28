"""The X draft must fit the wire, and be SCORED as it will SHIP — 2026-07-28.

content_publisher posts `content_text[:280]` to the X API. The pillars X draft
rendered at 359 chars, so what actually shipped was a fragment ending mid-phrase
("...get its DCPI") with the entire sign-off — CC-BY, "over MCP" and the LINK —
cut off. The quality gate never saw that: it scored the 359-char draft at 0.600
while the tweet that would really publish scored 0.150 and carried no link.

That is the same class of bug as the rest of this scorer's history — the thing
being evaluated was not the thing being published — and it is invisible in
every measurement that scores the draft.

Locked here:
  1. the X draft fits 280 when RENDERED, including when the live figures are a
     digit wider than today (dc_v/dc_t/ranked come from canonical stats),
  2. truncation is therefore a no-op: the scored text IS the shipped text,
  3. the draft clears the gate with margin, not on the threshold,
  4. it keeps a recency cue and a link — the two signals it used to lose,
  5. coverage_ratio does not read across a thousands separator.

Pure functions only; never imports main.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

cp = pytest.importorskip("content_publisher")  # noqa: E402
psm = pytest.importorskip("routes.pillars_master_shell")  # noqa: E402

# The X API truncation in content_publisher._post_to_twitter.
WIRE = 280

TODAY = {"dc_verified": 4923, "dc_tracked": 12650, "countries": 170,
         "markets": 311, "ranked_count": 24,
         "continents_ranked": ["NA", "EU", "AS", "SA", "OC"]}
# Every live figure a digit wider — the copy must not silently outgrow the wire
# as coverage grows.
BIGGER = {"dc_verified": 49238, "dc_tracked": 123650, "countries": 1700,
          "markets": 3110, "ranked_count": 240,
          "continents_ranked": ["NA", "EU", "AS", "SA", "OC", "AF", "AN"]}


@pytest.mark.parametrize("facts,label", [(TODAY, "today"), (BIGGER, "bigger")])
def test_x_draft_fits_the_wire(facts, label):
    x = psm._drafts(facts)["x"]
    assert len(x) <= WIRE, (
        "the X draft renders at %d chars with %s figures; the X API gets "
        "content_text[:%d], so %d chars — including the link — would be cut."
        % (len(x), label, WIRE, len(x) - WIRE))


@pytest.mark.parametrize("facts,label", [(TODAY, "today"), (BIGGER, "bigger")])
def test_what_is_scored_is_what_is_shipped(facts, label):
    """If these ever diverge the gate is judging a different artifact than the
    one that publishes, and every score below becomes meaningless."""
    x = psm._drafts(facts)["x"]
    assert x == x[:WIRE], "truncation changes the %s X post" % label


def test_x_draft_clears_the_gate_with_margin():
    """It sat at exactly 0.600 — on the threshold — where one word of copy
    drift silently re-blocks it. Margin is the point, not just passing."""
    x = psm._drafts(TODAY)["x"]
    score = cp._quality_score(x)
    assert score >= cp.QUALITY_MIN + 0.10, (
        "X draft scored %.3f, only %.3f above CONTENT_QUALITY_MIN"
        % (score, score - cp.QUALITY_MIN))


def test_x_draft_keeps_a_recency_cue_and_a_link():
    """The two signals the old copy lost — one to omission, one to truncation.
    'this month' is also what the LinkedIn, email and partner variants say."""
    x = psm._drafts(TODAY)["x"]
    assert cp._FRESHNESS_RE.search(x), "no recency phrase — freshness credit lost"
    assert cp._URL_RE.search(x) or cp._BARE_LINK_RE.search(x), "no link"


@pytest.mark.parametrize("key", ["linkedin", "x", "email", "partner_oneliner"])
def test_this_month_is_consistent_across_the_short_form_variants(key):
    """'this month' is true because the whole card is a monthly announcement —
    X was simply the only short-form variant that omitted it.

    `blog` is deliberately excluded: it is long-form, opens with its own
    headline, is never staged to social (pillars_stage_drafts stages linkedin +
    x only) and is never scored by _quality_score, so a recency phrase buys it
    nothing."""
    assert "this month" in psm._drafts(TODAY)[key].lower(), (
        "%s no longer says 'this month' — either the card stopped being "
        "monthly (fix the others too) or the recency cue was dropped" % key)


# ── the regex bug the new copy surfaced ────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("7 of 7 US ISOs report live demand", "coverage_ratio"),
    ("18 of 22 utilities filed on time", "coverage_ratio"),
    ("5 of 6 markets moved this week", "coverage_ratio"),
    # was matching as "923 of 12" and recording metric_value 923
    ("4,923 of 12,650 analyst-verified", None),
    ("1,204 of 12,650 tracked", None),
])
def test_coverage_ratio_does_not_read_across_a_thousands_separator(text, expected):
    assert cp._post_headline_signature(text)["metric_label"] == expected
