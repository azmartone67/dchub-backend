"""scraped_page_title.py — is this scraped <a> text a facility, or the page?

A provider-website scraper reads link text off a vendor's /data-centers page.
That page contains three kinds of link, and only one of them is a building:

    "Sterling, VA, NVA1-NVA3"   a facility        <- the thing we want
    "Amsterdam"                 a metro landing page
    "Equinix Smart Hands®"      a product / nav / footer link

`discovery_nexus.ProviderWebsitesSource` filtered link text on length and six
stop-words ('learn more', 'view all', 'see all', 'contact', 'menu', 'home'),
so the second and third kinds were ingested as facilities. Measured on the
live table 2026-08-08: of 312 `source='providerwebsites'` rows, 34 name no
place at all and 181 name a metro rather than a building.

This module holds the two predicates BOTH the crawler (reject at the door) and
the repair endpoint (classify what already landed) need, so the definition of
"not a facility" cannot drift between them.
"""
from __future__ import annotations

import re

# Nav / product / marketing link text, matched on the whole normalised string.
_NAV_EXACT = frozenset({
    "data centers", "data centres", "data center", "data centre",
    "all data centers", "all data centres", "data center locations",
    "explore data centers", "ai data centers", "all locations",
    "view locations", "see our design", "data center design",
    "edge strategy", "security & compliance", "sustainable data centers",
    "standards and compliance", "download our fleet tour guide",
    "cages and cabinets", "learn more", "view all", "see all", "contact",
    "menu", "home",
})

# Link text that only ever introduces a list of somewhere else.
_NAV_PREFIX = ("see our ", "see all ", "view all", "view locations",
               "explore ", "download ", "equinix smart", "equinix flex")

# Continents, super-regions, languages and whole countries. Each names a place
# — which is why a bare city-name check lets them through — but never a
# building, and never a DCPI market either.
_REGION_EXACT = frozenset({
    "americas", "emea", "apac", "asia pacific", "asia-pacific",
    "north america", "south america", "latin america", "europe",
    "europe, middle east & africa", "middle east & africa",
    "middle east and africa", "english", "french", "german", "spanish",
    "japanese", "portuguese", "united kingdom", "united states",
    "united states of america", "canada", "australia", "global", "worldwide",
})

# Two headings scraped as one string: a lowercase run butted straight against
# a capitalised word, with no space — "xScaleEnable multi-megawatt, AI-ready
# capacity" and "All LocationsAll Locations" are each a <h3> and its subtitle
# with the whitespace collapsed.
#
# ★ Operator brands are CamelCase too, and "DigitalBridge" is LEXICALLY
# IDENTICAL to a collapsed heading — same junction, same word lengths. No
# amount of pattern tuning separates them; only knowing it is a brand does.
# A bare `[a-z]{2}[A-Z][a-z]` search flagged the real facility
# "CyrusOne San Antonio, TX - SAT2-SAT4" as furniture.
#
# So this rule REQUIRES the caller to pass `provider`, which is stripped from
# the string first, and is skipped entirely when it is absent. Dropping a real
# facility is worse than keeping a junk row, and the two exact-match rules
# above carry the load on their own. Both live callers have the provider: it
# is the loop variable in the crawler and a column on the row in the repair.
# The 12-char floor on the junction token is a second belt — no operator brand
# in this scrape reaches it, every real collapsed heading clears it
# ("xScaleEnable" 12, "LocationsAll" 12, "excellenceStreamline" 20).
_RUNON = re.compile(r"[a-z]{2}[A-Z][a-z]")
_RUNON_MIN_TOKEN = 12

_ROMAN = re.compile(r"\b(?:I{1,3}|IV|VI{0,3}|IX|X)\b")
_TRAILING_DC = re.compile(r"\s+data\s+cent(?:er|re)s?$", re.I)


def _has_collapsed_heading(name: str, provider: str = "") -> bool:
    """Two headings scraped as one string. Returns False when `provider` is
    unknown — see _RUNON above for why this rule cannot run blind."""
    brand = (provider or "").strip()
    if not brand:
        return False
    n = re.sub(re.escape(brand), " ", name or "", flags=re.I)
    return any(len(t) >= _RUNON_MIN_TOKEN and _RUNON.search(t) for t in n.split())


def is_page_furniture(name: str, provider: str = "") -> bool:
    """True when the link text names no place at all — a product, a service, a
    nav label, a continent or a language. These are never a facility under any
    reading, so the crawler should drop them and the repair should suppress
    them.

    `provider` is optional but should be passed when known: it is what keeps a
    CamelCase operator brand from reading as a collapsed heading.
    """
    n = (name or "").strip()
    if not n:
        return True
    low = n.lower()
    if low in _NAV_EXACT or low in _REGION_EXACT:
        return True
    if low.startswith(_NAV_PREFIX):
        return True
    return _has_collapsed_heading(n, provider)


def has_building_grain(name: str) -> bool:
    """True when the name identifies a BUILDING rather than a metro.

    Three discriminators, all taken from how operators actually name sites:
      * a digit anywhere      — "LON1", "NVA1-NVA3", "Hillsboro 3", "CIN2"
      * a " - " sub-location  — "Atlanta - Alpharetta", "Denver - Englewood"
      * a trailing numeral    — "Ashburn I", "Milan II", "Frankfurt I"

    A bare "Amsterdam" has none of them and is a metro index page. Note this
    is a claim about the NAME, not about whether a facility exists there.
    """
    n = _TRAILING_DC.sub("", (name or "").strip())
    if not n:
        return False
    head = re.split(r"\s+-\s+", n)[0]
    if re.search(r"\d", n):
        return True
    if n != head:
        return True
    return bool(_ROMAN.search(head))


def leading_place(name: str) -> str:
    """The place the scraped title leads with — "Frankfurt" from
    "Frankfurt, FRA1", "Ashburn" from "Ashburn II, VA, United States".

    This is what makes the city repair possible without a gazetteer: the
    scraper lost the row's location into a page-locale string, but the link
    text it captured still carries it verbatim.
    """
    n = _TRAILING_DC.sub("", (name or "").strip())
    n = re.split(r"\s+-\s+", n)[0]
    n = n.split(",")[0].strip()
    n = _ROMAN.sub("", n).strip()
    return re.sub(r"\s{2,}", " ", n)
