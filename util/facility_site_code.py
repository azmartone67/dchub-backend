"""Operator site-code detection for facility page titles (2026-09-02).

Why: the 28-day GSC query grain (findings/3_seo.md, expansion #1) shows ~20
operator site-code queries — "interxion mad1" (pos 7.4), "iad14 data center"
(10.2), "fra28 data center" (10.4), "htl05" (10.7), "digitalrealty ewr12
piscataway" (10.9), "ewr10" (7.7), "dus2" (12.5) — sitting at position 6–13
with 13–37 impressions each and ZERO clicks. The pages exist; their titles
bury the code inside a long name ("Equinix FR5 - Frankfurt, KleyerStrasse —
Frankfurt, DE Data Center | ENTSOE-DE grid | DC Hub"). The searcher's own
words, `<Operator> <CODE>`, should lead the title.

There is no site-code column in the DB (checked 2026-09-02: no `site_code` /
`building_code` anywhere in the schema or the routes), so the code is read
CONSERVATIVELY from the name:

  * one all-caps token `[A-Z]{2,4}\\d{1,3}` on its own word boundary
    ("FR5", "MAD1", "IAD14", "FRA28", "HTL05", "(DFW2)"), never lower-case,
    never with a hyphen inside ("MAD-1" is left alone — it may be a suite),
  * exactly ONE distinct such token — two different codes is a campus or a
    range, and picking one would be a guess,
  * a small deny-list of prefixes that are road numbers, price zones,
    units or generic labels rather than site codes: "US1" is US Route 1,
    "SH130" is a Texas state highway, "NO1"/"SE1"/"DK1" are Nordic price
    zones, "DC1" is "Data Center 1", "AI" / "EU" / "IT" are words.

Everything else is untouched: a facility without a detectable code renders
exactly the title it rendered yesterday, and the slug / canonical are never
derived from anything in this module.
"""
from __future__ import annotations

import re

_CODE_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2,4})(\d{1,3})(?![A-Za-z0-9])")

# Prefixes whose <LETTERS><digits> form is something other than a site code.
DENY_PREFIXES = frozenset({
    # roads / routes ("US1", "SR2", "CR12", "FM1960", "SH130", "RT9", "HWY1")
    "US", "SR", "CR", "FM", "SH", "RT", "HWY", "RTE", "SS",
    # electricity price zones / grid labels ("NO1", "SE3", "DK1", "IT1")
    "NO", "SE", "DK", "IT", "ES", "PT", "FI",
    # units and specs ("MW1", "KV1", "GW2", "MVA2", "RPM")
    "MW", "KW", "GW", "KV", "MVA", "MWH", "KWH", "GB", "TB", "PB",
    # generic labels ("DC1" = "Data Center 1", "AI2", "EU1", "IO1", "TV1")
    "DC", "AI", "EU", "IO", "TV", "HD", "ID", "IP", "PC", "OS", "IX",
    # tiers / phases / halls written as codes ("TIER3", "PH2", "HALL1")
    "TIER", "PH", "HALL", "BLDG", "BLD", "UNIT", "ST", "RM",
    # cloud-region compass words ("US-EAST5", "EUROPE-WEST6")
    "EAST", "WEST", "NORTH", "SOUTH",
})


def detect_site_code(name: str | None) -> str | None:
    """Return the single unambiguous site code in `name`, else None."""
    if not name:
        return None
    found = []
    for m in _CODE_RE.finditer(name):
        letters, digits = m.group(1), m.group(2)
        if letters in DENY_PREFIXES:
            continue
        code = letters + digits
        if code not in found:
            found.append(code)
    if len(found) != 1:
        return None
    return found[0]


def site_code_headline(name: str | None, provider: str | None,
                       city: str | None) -> str | None:
    """`"<Operator> <CODE> — <City> Data Center"` when a code is detected and
    both an operator and a city are known; None otherwise (caller keeps its
    existing title). The operator is the name's own brand spelling when the
    words before the code are the provider's brand (so "Equinix FR5 …" stays
    "Equinix FR5", not "Equinix, Inc. FR5"); a brand in the name that is NOT
    the provider is kept after the provider ("Digital Realty Interxion MAD1")
    so the searcher's own words still appear contiguously."""
    code = detect_site_code(name)
    city = (city or "").strip()
    provider = (provider or "").strip()
    if not code or not city or provider.lower() == "operator":
        return None
    head, _sep, _tail = (name or "").partition(code)
    prefix = _clean_prefix(head, city)
    if provider and prefix and _same_brand(provider, prefix):
        operator = prefix
    elif provider and prefix:
        operator = f"{provider} {prefix}"
    elif provider:
        operator = provider
    elif prefix:
        operator = prefix
    else:
        return None
    return f"{operator} {code} — {city} Data Center"


def _clean_prefix(head: str, city: str) -> str:
    """Words before the code, minus the city and dangling punctuation."""
    words = [w.strip(" -–—:,.()[]|/") for w in head.split()]
    words = [w for w in words if w]
    if city:
        cw = [w.lower() for w in city.split()]
        if cw and len(words) >= len(cw) and \
                [w.lower() for w in words[-len(cw):]] == cw:
            words = words[:-len(cw)]
        words = [w for w in words if w.lower() not in cw]
    # Generic trailing words are not part of a brand.
    while words and words[-1].lower() in _GENERIC:
        words.pop()
    if len(words) > 4:
        return ""
    return " ".join(words)


_GENERIC = {"data", "center", "centre", "datacenter", "datacenters",
            "centers", "centres", "campus", "facility", "site", "the", "at",
            "in", "of", "and", "&"}


def _same_brand(provider: str, prefix: str) -> bool:
    p = re.findall(r"[a-z0-9]+", provider.lower())
    n = re.findall(r"[a-z0-9]+", prefix.lower())
    if not p or not n:
        return False
    if " ".join(n) in " ".join(p) or " ".join(p) in " ".join(n):
        return True
    return p[0] == n[0] and p[0] not in _GENERIC
