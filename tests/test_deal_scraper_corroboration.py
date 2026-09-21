"""deal_scraper must not publish a headline as a deal it does not state.

REAL FIXTURES, both sides of the defect:

  * ARTICLES — three items from the Bloomberg Tech RSS feed, captured
    2026-09-21 by deal_scraper's own fetch_rss_articles. Fed through the
    pre-fix article_to_deal they produced, byte for byte, the three rows that
    led keyless /api/v1/deals that evening: AUTO-5c0fee "Ares acquires
    Nvidia" (sh-ARES), AUTO-eba9cc "Microsoft acquires Anthropic", and
    AUTO-8527dc "Google invests in xAI".
  * STORED ROWS — the prod `deals` rows as written (notes = the headline the
    scraper stores), including AUTO-463c6f, whose summary is gone from the
    feed: that one can only be judged from what the row keeps.

The article tests import deal_scraper alone, so they fail by ASSERTION on the
pre-fix module, not by a missing import.
"""

import pytest

import deal_scraper


# Bloomberg Tech feed, captured 2026-09-21 (deal_scraper.fetch_rss_articles).
ARES_NVIDIA = {
    "title": "Nvidia Buying an Additional $1.5 Billion in SB Energy Shares Ahead of IPO",
    "summary": ("Nvidia Corp. is buying an additional $1.5 billion in shares of SB "
                "Energy Inc., a data center provider backed by SoftBank Group Corp., "
                "ahead of that company’s US initial public offering."),
    "published": "2026-09-21T17:23:31", "source": "Bloomberg Tech", "priority": 3,
}
MICROSOFT_ANTHROPIC = {
    "title": "Anthropic and Microsoft Dominate Nscale’s $103 Billion in Contracts",
    "summary": ("Nscale’s 1,000-fold increase in contracts over the last three "
                "years is mainly due to two clients thirsty for artificial intelligence "
                "computing capacity: Microsoft Corp. and Anthropic PBC."),
    "published": "2026-09-21T12:06:29", "source": "Bloomberg Tech", "priority": 3,
}
GOOGLE_XAI = {
    "title": "Former xAI Staffer Eyes $50 Million for Firm to Fight Deepfakes",
    "summary": ("A former xAI and Google staffer is in talks to raise roughly $50 "
                "million in seed funding for a new company focused on combating "
                "deepfakes online, according to a person familiar with the matter."),
    "published": "2026-09-21T17:31:11", "source": "Bloomberg Tech", "priority": 3,
}

# Same capture — real acquisitions the scraper must still publish (recall).
SLB_KELVION = {
    "title": "SLB to Acquire Kelvion, Expanding its Role Across Data Center Infrastructure - SLB",
    "summary": "", "published": "2026-08-31T07:00:00", "source": "SLB", "priority": 3,
}
ANTIN_NORTHC = {
    "title": "Antin To Buy NorthC From DWS In European Colocation Data Centre Deal - Pulse 2.0",
    "summary": "", "published": "2025-12-16T08:00:00", "source": "Pulse 2.0", "priority": 3,
}
# Same capture — an M&A headline with no target: the writer must refuse it.
COREWEAVE_TERMINATION = {
    "title": ("Core Scientific Announces Termination of Merger Agreement with "
              "CoreWeave - Core Scientific"),
    "summary": "", "published": "2025-10-30T07:00:00", "source": "Core Scientific", "priority": 3,
}
ARCUS_VOLTA = {
    "title": "Arcus to Acquire London Data Centre Volta from Verne - ET Datacenters",
    "summary": "", "published": "2026-07-04T07:00:00", "source": "ET Datacenters", "priority": 3,
}

# prod `deals` as stored 2026-09-21: (id, notes, buyer, seller, type).
STORED_BAD = [
    ("AUTO-eba9cc", MICROSOFT_ANTHROPIC["title"], "Microsoft", "Anthropic", "M&A"),
    ("AUTO-5c0fee", ARES_NVIDIA["title"], "Ares", "Nvidia", "M&A"),
    ("AUTO-8527dc", GOOGLE_XAI["title"], "Google", "xAI", "Equity"),
    ("AUTO-463c6f", "Singapore gov't allocates 200MW of power for four data center "
                    "developers on Jurong Island", "Equinix", "Digital Realty", "New Build"),
    # buyer is a substring of another word: fluid-STACK
    ("AUTO-41c63b", "AI data center builder Fluidstack raises $830M at $7.5B valuation "
                    "- SiliconANGLE", "Stack", None, "Equity"),
    # M&A with no seller — and the stored "buyer" is the target
    ("AUTO-f8c4c9", "Private Consortium Completes Acquisition of Aligned Data Centers - "
                    "the Electrical Distributor magazine", "Aligned", None, "M&A"),
    # parties fine, type invented: "Re-LEASE-d" (led the feed once 5c0fee went)
    ("AUTO-f3f13d", "Amazon Says AI Models Should Be Released When \u2018Ready and Safe\u2019",
                    "Amazon", None, "Lease"),
]
# The one two-party AUTO row of 829 whose headline states its pair.
STORED_GOOD = ("AUTO-20260701-e12026", "Digital Realty acquires Blackstone's stake in "
               "three Virginia data centers for $3.5 billion", "Digital Realty",
               "Blackstone", "M&A")


def test_the_captured_articles_are_deal_articles():
    # Otherwise the tests below would pass on a pre-filter, not the classifier.
    for a in (ARES_NVIDIA, MICROSOFT_ANTHROPIC, GOOGLE_XAI):
        assert deal_scraper.is_deal_article(a["title"], a["summary"], a["priority"])


def test_shares_is_not_ares_and_the_buyer_is_the_one_buying():
    deal = deal_scraper.article_to_deal(ARES_NVIDIA)
    assert deal is not None, "a real, stated share purchase must still be recorded"
    assert (deal["buyer"], deal["seller"]) == ("Nvidia", "SB Energy")
    assert deal["type"] == "Equity"          # buying shares, not acquiring the company
    assert deal["value"] == 1500.0           # "$1.5 billion", in millions


@pytest.mark.parametrize("article", [MICROSOFT_ANTHROPIC, GOOGLE_XAI],
                         ids=["microsoft-anthropic", "google-xai"])
def test_two_names_in_a_headline_are_not_a_deal(article):
    assert deal_scraper.article_to_deal(article) is None


def test_an_acquisition_without_a_target_is_not_published():
    assert deal_scraper.article_to_deal(COREWEAVE_TERMINATION) is None


@pytest.mark.parametrize("article,pair", [
    (SLB_KELVION, ("SLB", "Kelvion")),
    (ANTIN_NORTHC, ("Antin", "NorthC")),
    (ARCUS_VOLTA, ("Arcus", "Volta")),
], ids=["slb-kelvion", "antin-northc", "arcus-volta"])
def test_stated_acquisitions_are_still_published_as_ma(article, pair):
    deal = deal_scraper.article_to_deal(article)
    assert deal is not None
    assert (deal["buyer"], deal["seller"], deal["type"]) == (*pair, "M&A")


def test_an_actor_is_named_as_a_whole_word():
    # AUTO-41c63b was stored buyer="Stack" off "Fluid-STACK".
    assert deal_scraper.extract_companies(STORED_BAD[4][1]) == (None, None)


def test_no_default_ma_and_no_substring_types():
    assert deal_scraper.parse_deal_type(MICROSOFT_ANTHROPIC["title"]) is None
    assert deal_scraper.parse_deal_type("Meta Releases AI Model You Can Use at Home") is None
    assert deal_scraper.parse_deal_type("Emerging markets draw data center capital") is None


def test_billion_is_parsed_as_billion():
    # The `?` -> `%s` corruption made "$103 Billion" fall through to the raw
    # "$103" branch and store 103 ($ millions) on AUTO-eba9cc.
    assert deal_scraper.parse_value_millions("Nscale’s $103 Billion in Contracts") == 103000.0
    assert deal_scraper.parse_value_millions("raise roughly $50 million in seed") == 50.0


def test_region_needs_a_whole_word():
    # AUTO-eba9cc was stored region=APAC: "computing c-APAC-ity" in the summary.
    text = MICROSOFT_ANTHROPIC["title"] + " " + MICROSOFT_ANTHROPIC["summary"]
    assert deal_scraper.detect_region(text) == "Global"


def test_new_rows_carry_their_source_url():
    deal = deal_scraper.article_to_deal({**SLB_KELVION, "url": "https://example.test/slb"})
    assert deal["source_url"] == "https://example.test/slb"


# ── the stored-row predicate the quarantine sweep uses ──────────────────────

@pytest.mark.parametrize("row", STORED_BAD, ids=[r[0] for r in STORED_BAD])
def test_stored_misparses_are_not_corroborated(row):
    from util.deal_corroboration import is_corroborated
    _id, notes, buyer, seller, deal_type = row
    assert not is_corroborated(notes, buyer, seller, deal_type)


def test_a_stated_pair_is_corroborated():
    from util.deal_corroboration import is_corroborated
    _id, notes, buyer, seller, deal_type = STORED_GOOD
    assert is_corroborated(notes, buyer, seller, deal_type)
    # Same headline, pair reversed: direction is part of the claim.
    assert not is_corroborated(notes, seller, buyer, deal_type)


def test_passive_voice_keeps_direction():
    # Not from the feed (no passive headline in the 140 captured): pins the
    # direction of "<seller> acquired by <buyer>".
    from util.deal_corroboration import extract_directional_pair, is_corroborated
    headline = "AirTrunk Acquired by Blackstone and CPP Investments"
    assert extract_directional_pair(headline) == ("Blackstone", "AirTrunk")
    assert not is_corroborated(headline, "AirTrunk", "Blackstone", "M&A")


def test_writer_and_sweep_agree_on_what_the_writer_publishes():
    from util.deal_corroboration import is_corroborated
    for article in (ARES_NVIDIA, SLB_KELVION, ANTIN_NORTHC, ARCUS_VOLTA):
        d = deal_scraper.article_to_deal(article)
        assert is_corroborated(d["notes"], d["buyer"], d["seller"], d["type"]), d["id"]
