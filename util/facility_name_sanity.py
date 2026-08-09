"""facility_name_sanity.py — is this "facility name" actually a news headline?

Single source of truth for the question the news ingestion paths never asked:
*is the string we are about to publish as a data-center name a facility at
all, or is it the headline we scraped it out of?*

WHAT WENT WRONG
---------------
Three separate news paths write rows into `discovered_facilities`, and every
one of them uses the article title (or an NER span cut out of it) as the
facility `name`:

    news_facility_extractor.extract_facility_from_article
        'name': title[:200]        # "Use article title as facility name"
    routes/news_entity_extraction._promote_candidates
        facility["name"] = name    # a Tier-1 regex / Haiku NER span
    (historical) source='news_pipeline'

Those rows drain into `facilities`, get a frozen slug, and render a live 200
page with `robots=index, follow`, a self-canonical, and a sitemap entry.
Measured 2026-08-09 against the live corpus (20,132 `facilities` +
25,024 `discovered_facilities` rows):

    /facilities/stack-breaks-ground-on-second-tokyo-data-center-900992d1
        <title>Stack breaks ground on second Tokyo data center Data Center …
    /facilities/12-billion-data-center-breaks-ground-in-cheyenne-…-oil-city-news-…
        …the slug carries the PUBLICATION name
    /facilities/copilot-07a85c97        <title>Copilot — US Data Center …
    /facilities/ferc-ferc-9e0a2b63      <title>FERC — US Data Center …

WHY IT IS NOT A SLUG PROBLEM
----------------------------
These carry no structural marker in the URL, so no slug regex can find them
without eating real facilities. Proof, from the 08-09 sitemap-prune work:
`data-center-<6+ digits>` reads like OSM junk and matches 25 REAL live slugs
whose frozen 8-hex hash merely begins with digits (equinix-atlanta-data-
center-144841dc, ibm-annapolis-data-center-4498886f);
tests/test_seo_index_hygiene.py::test_sitemap_junk_guard_spares_real_facilities
pins them. The signal lives in the NAME, so the predicate lives here.

THE TWO PREDICATES
------------------
`headline_reject_reason(name)` — the name reads as a sentence, not a name.
    Announcement verbs, groundbreaking/market-report phrases, a trailing
    publication ("… - Oil City News"), a trailing bare "Unknown", or
    sentence-length with function words. Serve-side safe: it is the ONLY
    thing allowed to de-index an existing row.

`evidence_reject_reason(fac)` — the row carries no location or physical
    evidence at all: no city, no address, no coordinates, no MW, no sqft.
    INGEST-TIME ONLY. Do NOT use it to de-index existing rows: 45 live
    OpenStreetMap facilities with real names (AiNET, "Amazon Web Services
    CMH50") carry none of those fields in our copy, and de-indexing them
    would be exactly the collateral damage the slug regex was rejected for.

`facility_reject_reason(fac)` — ingestion gate; both of the above.

CALIBRATION (measured 2026-08-09, whole live corpus, both tables)
-----------------------------------------------------------------
`headline_reject_reason` flags 89 of 20,132 `facilities` rows and 38 of
25,024 `discovered_facilities` rows. Every single hit comes from a news
ingestion source (news_pipeline / news_extraction / news_ner / the
dchub_pipeline NER batch of 2026-05-04). ZERO hits on PeeringDB,
OpenStreetMap, competitor_gap, manual_seed, datacentermap or any other
registry source — the four rules below were each trimmed until that was
true, and the trims are the interesting part:

  · a "name contains a comma and is long" rule was DROPPED outright: it hit
    'ATMAN Data Center Warsaw-2 (WAW-2, Konstruktorska 5)',
    'Equinix TR7 - Toronto, Brampton (formerly Q9 Brampton 4/5)' and
    'Ark Data Centres - Spring Park - SQ17, P1, P2, P3, P4, P5'.
  · "eyes" left the verb list for 'PIX Eyes Nwhere' (a real Manaus IX).
  · "acquired" left it for 'Google Intersect Power Sites (Acquired)'.
  · "business" left the publication-word list for 'CDC3 - 800 E Business
    Center Dr' and three real 'PŸUR Business' sites in Leipzig/Berlin.
  · the tokenizer is UNICODE (`[^\\W_]+`), not `[a-z0-9]+`: the ASCII form
    shredded 'Belügyminisztérium Nyilvántartások Vezetéséért Felelős
    Helyettes Államtitkárság' into 15 fragments and tripped the length rule
    on a real Budapest facility.

★ Any widening of these lists MUST be re-measured against the live name
  corpus the same way. A false ACCEPT costs one junk page; a false REJECT
  silently de-indexes a real facility, and slugs are FROZEN so it cannot be
  undone by renaming.
"""

import re

# Third-person announcement verbs. A data-center NAME essentially never
# contains one; a headline almost always does. Verified: zero hits across
# 45,156 live names outside the news sources.
_HEADLINE_VERBS = frozenset("""
acquires announces announced approves begins breaks broke builds buys
celebrates closes commits completes confirms delays denies develops doubles
expands files halts invests joins launches launched leases opens passes
pauses picks plans prepares promises proposes raises rejects reveals says
secures seeks selects sells signs starts taps unveils unveiled upgrades
weighs wins withdraws
""".split())

# Multi-token shapes that survive without a finite verb — infinitives
# ("Barcelona to build"), gerunds, and the market-report title family.
_HEADLINE_PHRASES = frozenset([
    "breaks ground", "broke ground", "breaking ground", "groundbreaking",
    "to build", "to develop", "to open", "to invest", "to launch",
    "to expand", "to acquire", "to add", "to deploy", "to power",
    "to be replaced", "will build", "will develop", "will open",
    "will invest", "it will",
    "market size", "market analysis", "market supply", "market outlook",
    "forecast report", "outlook forecast", "growth report",
])

# Words that mark the trailing segment after " - " / " | " as a publication
# rather than a building or a street. Deliberately excludes "business" —
# see the calibration note above.
_PUBLICATION_WORDS = frozenset("""
news times post journal daily herald tribune gazette chronicle sentinel
advocate ledger courier newsroom wire newswire globenewswire magazine review
report today monitor observer dispatch insider insights press bloomberg
reuters cnbc techcrunch datacenterdynamics frontier knowledge week wired
axios verge
""".split())

# Function words. Their presence in a long string is what separates a
# sentence from a long proper name ("Digital Realty Av 2 50 Quadra G1
# Distrito Industrial Benedito Storani Bldg 2" has none and is real).
_FUNCTION_WORDS = frozenset("""
a an the in on at for from with after as by over about into during against
is are was were be been has have had would could should will can may
it its their our your his her they we you that this these those
and or but not no more most than then when where what how why who which
""".split())

# ★ UNICODE word tokenizer. `[a-z0-9]+` shreds accented names — see above.
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)

# " - Foo Bar" / " | Foo Bar" tail, capturing the trailing segment only.
_TRAILING_SEGMENT_RE = re.compile(r"\s[-–—|]\s*([^-–—|]{2,60})$")

_MIN_SENTENCE_TOKENS = 12


def _tokens(text):
    return _TOKEN_RE.findall((text or "").lower())


def headline_reject_reason(name):
    """Return a short reason string when `name` reads as a news headline or
    sentence fragment rather than a facility name, else None.

    Safe for serve-side use (noindex / sitemap exclusion): calibrated to
    zero false positives across the whole live name corpus.
    """
    text = (name or "").strip()
    if not text:
        return None
    tokens = _tokens(text)
    if not tokens:
        return None
    joined = " ".join(tokens)

    for phrase in _HEADLINE_PHRASES:
        if phrase in joined:
            return "headline-phrase:" + phrase

    for token in tokens:
        if token in _HEADLINE_VERBS:
            return "headline-verb:" + token

    # '<provider> <name> Unknown' — the 2026-05-04 NER batch. The 08-09
    # sitemap prune caught these with a '-unknown-<hash8>$' slug regex;
    # keyed on the NAME it also covers ingestion and the profile page.
    if tokens[-1] == "unknown":
        return "unknown-tail"

    match = _TRAILING_SEGMENT_RE.search(text)
    if match:
        segment = _tokens(match.group(1))
        if segment and len(segment) <= 6 and any(
                word in _PUBLICATION_WORDS for word in segment):
            return "publication-suffix:" + match.group(1).strip()[:40]

    if (len(tokens) >= _MIN_SENTENCE_TOKENS
            and any(word in _FUNCTION_WORDS for word in tokens[1:])):
        return "sentence-length:%d" % len(tokens)

    return None


def _blank(value):
    return value is None or str(value).strip() == ""


def _zeroish(value):
    """True when a numeric-ish field carries no information. sqft is TEXT in
    discovered_facilities and INTEGER in facilities, so parse defensively."""
    if value is None:
        return True
    if isinstance(value, str):
        digits = re.sub(r"[^0-9.]", "", value)
        if not digits:
            return True
        try:
            return float(digits) == 0.0
        except ValueError:
            return True
    try:
        return float(value) == 0.0
    except (TypeError, ValueError):
        return True


def evidence_reject_reason(fac):
    """Return a reason when a candidate row carries NO location or physical
    evidence whatsoever, else None.

    ★ INGEST-TIME ONLY. 45 real OpenStreetMap facilities in the live table
    would fail this; never use it to de-index an existing row.
    """
    if not isinstance(fac, dict):
        return None
    if not _blank(fac.get("city")):
        return None
    if not _blank(fac.get("address")):
        return None
    if not _blank(fac.get("market")):
        return None
    for lat_key, lon_key in (("latitude", "longitude"), ("lat", "lon")):
        if not _blank(fac.get(lat_key)) and not _blank(fac.get(lon_key)):
            return None
    for key in ("power_mw", "sqft", "acreage", "investment_usd"):
        if not _zeroish(fac.get(key)):
            return None
    return "no-location-evidence"


def facility_reject_reason(fac):
    """Ingestion gate: return a reason to refuse this candidate, else None.

    Both prongs, keyed on `name` ONLY. Running the headline predicate over
    `provider` as well was measured and rejected: it flags 1,620 more live
    rows, almost all the OpenStreetMap `provider='Unknown'` family (whose
    NAME is fine and which the slug-level guards already handle), plus three
    real PeeringDB universities whose provider is a full department title
    ('Faculty of Computer Science and Engineering, Ss. Cyril and Methodius
    University in Skopje'). Callers should log the reason — a silent drop
    here is indistinguishable from "the source had nothing new".
    """
    if not isinstance(fac, dict):
        return None
    return (headline_reject_reason(fac.get("name"))
            or evidence_reject_reason(fac))
