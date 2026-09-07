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

THE DESIGNATOR (2026-09-07, r-site-code-tail)
--------------------------------------------
Reducing the name to that one code is LOSSY when the name carries a building
or hall designator ON TOP of the code. Measured 2026-09-07 against the live
publishable universe (39,739 rows, both tables, junk slugs excluded): 237
rendered-identity groups hold >=2 URLs whose NAMES differ, 229 of them because
every member takes this path. Confirmed distinct buildings collapsing to ONE
`<h1>`:

    'SecureIT DCB1.1'           'SecureIT DCB1.2'          0.00 km apart
    'noris network AG ING1 ITA' 'noris network AG ING1 ITB' 0.03 km
    'Centersquare IAD1-A'  '-B'  '-C'                       one h1 for three
    'RIC3 DC1' .. 'RIC3 DC5'                                one h1 for five

64 of those pairs publish two live sitemap URLs, so the collapse is a
duplicate-content defect in its own right, independent of dedup. So the code
is extended by a designator when — and ONLY when — the name attaches one
directly to it, in one of the two shapes the corpus actually uses:

  * GLUED, no whitespace: a separator then short alphanumeric groups —
    ".1", ".6", "-A", "-3", "-A/B/C", "/2/3/4", "-11-12". "FR2.6" and
    "DCB1.1" keep their extension; "FR5 - Frankfurt, KleyerStrasse" does not
    (the separator is followed by a space, so nothing matches).
  * ONE TRAILING WORD: the remainder of the name is exactly one short
    all-caps token — " ITB", " DC1". It must be the LAST thing in the name,
    which is what keeps "UIH BCH4 IDC - Bangkok, Thailand" out, and it must
    not be a legal/generic word (DENY_SUFFIX_WORDS). The whole live corpus
    holds ten such tokens: DC1-DC5 and ITA/ITB are designators, LLC / CO / DC
    are not.

Measured effect of the designator: 158 of the 5,586 rows on this path change
their `<h1>`, every one of them gaining a building designator and none of them
gaining a location word. No row LOSES a code, and a name with no designator
renders byte-identically to yesterday — including every query the rationale
above was built on ("interxion mad1", "iad14", "fra28", "htl05", "dus2"),
whose names carry no designator at all.

routes/facility_dedup_v4.py groups on util.facility_headline.identity_key, so
this moves its scan. Both runs on one snapshot, 2026-09-07:

    unlinkable_legacy   510 -> 500   the residual it exists to report shrinks
    writable             24 ->  17   SEVEN merges it would have WRITTEN are
                                     now refused — and all seven are the pairs
                                     above (noris ITA/ITB, FR2/FR2.6, DCB1.1/
                                     .2, FR8.1/.2, MRS1/MRS1-3, DUB1/DUB1-2,
                                     RIC1 DC1/DC2/DC3). A false merge hides a
                                     real facility, so that is the safety half.
    group_too_large       5 ->   4   RIC3 DC1..DC5 stops being one 5-URL group

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


# ── the designator that rides ON the code ────────────────────────────
# Glued to the code with no whitespace: ".1" "-A" "-3" "-A/B/C" "/2/3/4".
# Upper-case/digits only, so "-Frankfurt" and "- Frankfurt" both fail; the
# trailing boundary is what stops "FR5-F" being read out of "FR5-Frankfurt".
_SUFFIX_GLUED_RE = re.compile(
    r"^([.\-/][A-Z0-9]{1,3}(?:[/\-][A-Z0-9]{1,3})*)(?![A-Za-z0-9])")

# The entire remainder of the name is one short all-caps word: " ITB", " DC1".
# Anchored at BOTH ends on purpose — a token with anything after it is prose
# ("UIH BCH4 IDC - Bangkok, Thailand"), not a designator.
_SUFFIX_WORD_RE = re.compile(r"^ ([A-Z]{2,4}\d{0,3})$")

# Trailing words that are NOT a building. Measured live 2026-09-07: the whole
# corpus of single-word tails is {DC1..DC5, ITA, ITB} (designators) and
# {LLC, CO, DC} (not) — the three below are the measured ones, the rest are
# the same class of legal/generic token and cost nothing to name up front.
DENY_SUFFIX_WORDS = frozenset({
    # measured live
    "LLC", "CO", "DC",
    # legal-form siblings of LLC/CO
    "INC", "LTD", "CORP", "GMBH", "AG", "BV", "NV", "SA", "SAS", "SRL",
    "SPA", "PTE", "PTY", "PLC", "LP", "LLP", "KK", "AB", "AS", "OY", "SL",
    # generic facility words written as an acronym
    "IDC", "IXP", "POP", "NOC", "MMR", "HQ", "CLS",
})


def _designator_suffix(name: str, code: str, city: str = "") -> str:
    """The building/hall designator attached to `code` in `name`, else ""."""
    tail = (name or "").partition(code)[2]
    if not tail:
        return ""
    m = _SUFFIX_GLUED_RE.match(tail)
    if m:
        return m.group(1)
    m = _SUFFIX_WORD_RE.match(tail)
    if not m:
        return ""
    word = m.group(1)
    if word in DENY_SUFFIX_WORDS:
        return ""
    # a trailing city word is a location, not a building ("EQUINIX LD8 UK"
    # style country/city tokens must not survive into "<City> Data Center")
    if word.lower() in {w.lower() for w in (city or "").split()}:
        return ""
    return " " + word


def detect_site_designator(name: str | None, city: str = "") -> str | None:
    """`detect_site_code` plus any designator riding on it: "DCB1.1",
    "IAD1-A", "ING1 ITB". None whenever `detect_site_code` is None — the
    designator NEVER creates a headline that would not otherwise exist."""
    code = detect_site_code(name)
    if not code:
        return None
    return code + _designator_suffix(name or "", code, city)


def site_code_headline(name: str | None, provider: str | None,
                       city: str | None) -> str | None:
    """`"<Operator> <CODE> — <City> Data Center"` when a code is detected and
    both an operator and a city are known; None otherwise (caller keeps its
    existing title). <CODE> carries its building designator when the name
    attaches one ("SecureIT DCB1.1", "Centersquare IAD1-A", "noris network AG
    ING1 ITB") — see `_designator_suffix`; without one it is the bare code,
    exactly as before. The operator is the name's own brand spelling when the
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
    # r-site-code-tail: what LEADS the headline is the code plus whatever
    # building designator the name attaches to it, so two halls of one campus
    # do not render one <h1>. `head` is still split on the bare code.
    ident = code + _designator_suffix(name or "", code, city)
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
    return f"{operator} {ident} — {city} Data Center"


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
