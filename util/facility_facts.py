"""What a facility page states first, and what changed in its record.

r-facility-facts (2026-09-15), SEO Step 1. Facility pages earn 658 of 753
page-attributed clicks (GSC, 28 days to 09-15) at 0.73% CTR, and page-one
owner and address lookups get none: "kanobe llc bothell data center owner" sat
at position 5.0 on a page whose snippet named neither its operator nor an
address. The page and its meta description now state the operator, the street
address and the status, each ONLY when the stored value is a real one, plus
the changes `entity_changes` has recorded for the row.

DISPLAY-ONLY, like util.facility_headline: nothing here writes, and
identity_key() reads none of it.

Measured on 3,000 live pages sampled from the three facility sitemaps
(2026-09-15), which is where every rule below comes from:

    streetAddress stored      351 (11.7%)
      published by this rule  127  (4.2%)  "1950 N Stemmons Fwy",
                                           "Stekkenbergweg", "Calle 31"
      refused                 224          "India", "GB", "Chicago",
                                           "Mumbai, India", "Australia on",
                                           "Japan to", "MW", "it has"
    provider line "Operator"  661 (22.0%)  the renderer's placeholder,
                                           printed as if it were a name
    status known            2,973 (99.1%)

So a value is an address when it names a street or carries a house number,
and nothing else is: a place name, a country code or a fragment of news prose
is not published as one, however it was stored.
"""
from __future__ import annotations

import datetime
import math
import re

from util.facility_headline import display_mw, plausible_mw
from util.thin_content import is_placeholder_city

# Stored markers for "we do not know". Compared on the whole, lower-cased value.
# "operator" is what routes/facility_profile_page substitutes for an empty
# provider, so it arrives here looking like a name.
PLACEHOLDER_VALUES = frozenset((
    "", "operator", "unknown", "n/a", "na", "none", "null", "tbd", "tba",
    "other", "various", "-", "--", "?", "0",
))


def _clean(value) -> str:
    if value is None or isinstance(value, bool):
        return ""
    return " ".join(str(value).split())


def real_operator(provider, name=None) -> str:
    """The operator to name, or "" for a placeholder or the facility's own name.

    Equality with the name, never containment: "Bothell Data Services" operates
    "Kanobe, LLC", and "Google" is still worth stating on "Google Council
    Bluffs Data Center". A provider that merely repeats the name says nothing
    about who runs the building."""
    op = _clean(provider)
    if op.lower() in PLACEHOLDER_VALUES:
        return ""
    if name is not None and op.lower() == _clean(name).lower():
        return ""
    return op


def real_status(status) -> str:
    """The lifecycle status as the page's Status tile prints it, or ""."""
    st = _clean(status)
    return "" if st.lower() in PLACEHOLDER_VALUES else st.title()


def is_fleet_row(power_mw) -> bool:
    """True when the row carries a real, positive capacity that plausible_mw
    refuses: a utility's generating fleet ("AEP None", 63,000 MW), not a site.

    Such a row gets none of the facts in this module. ASKS plausible_mw rather
    than re-spelling the cap (see its docstring for why that matters)."""
    if isinstance(power_mw, bool):
        return False
    try:
        p = float(power_mw)
    except (TypeError, ValueError):
        return False
    return math.isfinite(p) and p > 0 and plausible_mw(p) is None


# ── street address ──────────────────────────────────────────────────────────

_WORDS = re.compile(r"[^\W\d_]+")
# The street type is the LAST word of an English, Slavic or Turkish street
# name and the FIRST word of a Romance one. Anywhere else it proves nothing:
# "St. Louis, Missouri" is a city.
_STREET_LAST = frozenset((
    "street", "st", "road", "rd", "avenue", "ave", "drive", "dr", "boulevard",
    "blvd", "court", "ct", "lane", "ln", "way", "parkway", "pkwy", "highway",
    "hwy", "freeway", "fwy", "expressway", "expy", "circle", "cir", "terrace",
    "square", "crescent", "trail", "pike", "plaza", "loop", "turnpike",
    "close", "ulica", "улица", "caddesi", "sokak", "marg", "salai", "utca",
    "út",
))
_STREET_FIRST = frozenset((
    "rue", "avenue", "avenida", "av", "calle", "carrera", "carretera", "rua",
    "rodovia", "estrada", "travessa", "alameda", "via", "viale", "corso",
    "piazza", "strada", "jalan", "jl", "ulica", "ul", "улица", "ул", "boulevard",
    "chemin", "route", "quai", "paseo", "camino",
))
# Germanic and Nordic street names are one compound word: "Stekkenbergweg".
_STREET_SUFFIX = ("strasse", "straße", "str", "weg", "laan", "straat", "gasse",
                  "platz", "allee", "vej", "gatan", "vägen", "veien", "katu",
                  "kuja", "aukio", "vegur")
_STREET_CJK = ("路", "街", "大道", "號", "号", "丁目", "번지")
# A measurement or an amount that happens to sit beside words: "100 MW campus".
_MEASURE = re.compile(
    r"\b(?:mw|gw|kw|mva|megawatts?|gigawatts?|sq\.?\s*ft|sqft|acres?|hectares?"
    r"|square\s+(?:feet|foot|met(?:er|re)s?)|billion|million|percent)\b", re.I)
# English words of the prose a news extractor lifts ("it has", "Arizona
# through", "near the Bath Road"). A street name carrying one is published
# only with a house number: "1301 Avenue of the Americas".
_PROSE = frozenset((
    "it", "has", "have", "had", "is", "was", "are", "were", "will", "be",
    "the", "for", "through", "of", "in", "to", "and", "near", "by", "from",
    "with", "its", "this", "that", "which", "located", "into", "as",
))
_UNKNOWN_WORDS = frozenset(("unknown", "none", "null", "tbd", "tba", "na"))
# Parts of a site, not of a street: "Data Hall 3".
_SITE_PARTS = frozenset((
    "phase", "stage", "tier", "building", "bldg", "hall", "unit", "campus",
    "site", "zone", "level", "floor", "suite", "room", "pod", "data", "dc",
))
# What a leading number COUNTS when it is not a house number: "3 Data Halls",
# "300 jobs created".
_COUNTED = frozenset((
    "new", "data", "jobs", "year", "years", "month", "months", "buildings",
    "facilities", "centers", "centres", "halls", "sites", "people",
    "employees",
))
_MAX_ADDRESS = 200
_NUMBER = r"\d{1,6}[a-z]?(?:[-/]\d{1,6}[a-z]?)*"
_NUMBER_FIRST = re.compile(rf"^(?:no\.?\s*)?{_NUMBER},?\s+(.+)$", re.I)
_NUMBER_LAST = re.compile(rf"^(.+?)\s+{_NUMBER}$", re.I)


def _names_a_street(value: str) -> bool:
    if any(ch in value for ch in _STREET_CJK):
        return True
    for segment in value.split(","):
        words = [w.lower() for w in _WORDS.findall(segment)]
        # One word names a street only beside a number ("Calle 31",
        # "20544 HIGHWAY 370"); alone ("Street") it names nothing.
        if words and (words[-1] in _STREET_LAST or words[0] in _STREET_FIRST) \
                and (len(words) >= 2 or any(ch.isdigit() for ch in segment)):
            return True
        if any(len(w) > len(sfx) + 2 and w.endswith(sfx)
               for w in words for sfx in _STREET_SUFFIX):
            return True
    return False


def _has_house_number(value: str) -> bool:
    """A number beside at least two words of the first segment: "3301 Monte
    Villa Parkway", "Tehnološki park 21". One word is a postal district
    ("Dublin 15", "40549 Düsseldorf"), not a street."""
    head = value.split(",")[0].strip()
    first = _NUMBER_FIRST.match(head)
    if first:
        words = [w.lower() for w in _WORDS.findall(first.group(1))]
        return len(words) >= 2 and words[0] not in _COUNTED
    last = _NUMBER_LAST.match(head)
    return bool(last) and len(_WORDS.findall(last.group(1))) >= 2


def street_address(value) -> str:
    """The stored address when it is a street address, else "".

    Published ONLY when it names a street or carries a house number. Refused:
    place names and codes ("India", "GB", "Mumbai, India"), news prose ("it
    has", "near the Bath Road"), placeholders ("Unknown Street"), measurements
    ("100 MW campus"), counts ("300 jobs created") and site parts ("Data Hall
    3")."""
    v = _clean(value).strip(" ,;")
    if len(v) > _MAX_ADDRESS:
        return ""
    words = [w.lower() for w in _WORDS.findall(v)]
    if _UNKNOWN_WORDS.intersection(words) or set(words) <= _SITE_PARTS:
        return ""
    if _MEASURE.search(v):
        return ""
    street, numbered = _names_a_street(v), _has_house_number(v)
    if _PROSE.intersection(words) and not (street and numbered):
        return ""
    return v if (street or numbered) else ""


# ── the street NAME, without the number ────────────────────────────────────
#
# r-location-gate (2026-09-21). A facility's exact location is paid-only, and
# the server-rendered page is edge-cached and identical for every visitor, so
# what it may print of an address is the street NAME: never the house or
# building number, a unit, a plot or a postcode. street_address() above still
# decides WHETHER a stored value is an address; this decides how much of one
# may be published. It removes; it never validates, so a bare city comes back
# as itself and callers that need a street ask street_address() first.
#
# Over-stripping is the accepted failure: "Avenida 9 de Julio" loses its 9.
# The only digits that can survive are an ordinal ("8th Avenue") and the
# number of a route whose name is nothing but a road word ("Calle 31",
# "Highway 370"): there the number IS the street's name, and no house number
# can precede it without a street name of its own in between.

_DIRECTIONALS = frozenset((
    "n", "s", "e", "w", "ne", "nw", "se", "sw", "north", "south", "east",
    "west", "northeast", "northwest", "southeast", "southwest",
))
# Each takes the identifier after it with it: "Suite 100", "Unit B", "Plot 7",
# "No. 8", "# 4-15", "km 12".
_UNIT_WORDS = frozenset((
    "unit", "units", "suite", "ste", "apt", "apartment", "flat", "floor",
    "fl", "flr", "level", "lvl", "room", "rm", "bldg", "building", "bld",
    "tower", "wing", "house", "office", "ofc", "hall", "door", "dept", "box",
    "pmb", "plot", "lot", "stand", "erf", "kav", "kavling", "lt", "lantai",
    "sector", "phase", "stage", "shop", "bay", "dock", "hangar", "gate", "km",
    "no", "nr", "nro", "num", "numero", "número", "n°", "nº", "#",
))
# A block code may be letters only: "Blok Bi".
_BLOCK_WORDS = frozenset(("block", "blk", "blok"))
# A numbered lane off a main road is a house number along that road ("Lane
# 3111, X Road"), so it goes with its number when a street name follows it.
_LANE_WORDS = frozenset(("lane", "alley"))
_FLOOR_WORDS = frozenset(("floor", "fl", "flr", "level", "lvl", "storey",
                          "story"))
# Between two numbers they make one: "4762 AND 4764", "12 - 14", "12 & 14".
_NUMBER_JOINERS = frozenset(("-", "–", "—", "&", "+", "/", "and", "to", "thru",
                             "through", "y", "e", "et", "und"))
_NUMBER_SUFFIXES = frozenset(("bis", "ter", "quater"))
_EDGE_JUNK = frozenset(("and", "&", "+", "-", "–", "—", "/", "#", ",", ";",
                        ":", "."))
_ORDINAL = re.compile(r"^\d{1,4}(?:st|nd|rd|th)[.,]?$", re.I)
_HOUSE_NUMBER = re.compile(r"^#?\d{1,6}[a-z]?(?:[-–—/]\d{1,6}[a-z]?)*[.,:]?$",
                           re.I)
_ROUTE_NUMBER = re.compile(r"^(?:[a-z]{1,3}-?)?\d{1,5}[a-z]?$", re.I)
_ROUTE_NAME = re.compile(
    r"^(?:(?:n|s|e|w|north|south|east|west)\.?\s+)?"
    r"(?:(?:state|county|us|u\.s\.|farm\s+to\s+market|ranch\s+to\s+market"
    r"|provincial|national|federal)\s+)?"
    r"(?:highway|hwy|route|rte|rt|road|rd|street|st|interstate|calle|cl"
    r"|carrera|cra|kr|avenida|av|avenue|ave|rodovia|carretera|ruta|estrada"
    r"|autopista|autovia|strada|via|fm|rm|sr|cr|us|lane|ln)\.?$", re.I)
_HAS_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")
_CJK_NUMBER = re.compile(
    r"[0-9０-９]+\s*(?:号楼|號樓|番地|丁目|番|号|號|弄|楼|樓|室|层|層|栋|棟|번지|번길|호)?")
# "No.8" / "Str.5" / "#12" / "Nº5" -> the number as its own token.
_GLUED_ABBREV = re.compile(r"(?<=[^\W\d_])\.(?=\d)")
_GLUED_MARK = re.compile(r"(?i)(?:(?<=\s)|^)(#|n[°º]|no|nr)(?=\d)")
# Parts of an address: commas, semicolons, brackets, a spaced dash, a pipe.
_ADDRESS_PARTS = re.compile(r"[,;()\[\]|]|\s[-–—]\s")
# Words that name no place on their own: "Street", "N", "de la". A unit word
# that is still there was not taking an identifier, so it is part of a name
# ("Hall Road", "Dock Street") and is deliberately not listed.
_NOT_A_NAME = (_STREET_LAST | _STREET_FIRST | _DIRECTIONALS
               | frozenset(("and", "of", "the", "de", "la", "du", "del", "des",
                            "di", "da", "do", "no", "nr", "po", "p", "o")))


def _is_identifier(token: str) -> bool:
    """What a unit word takes with it: anything carrying a digit, or one
    letter ("Suite B"). Never a word: "Hall Road" keeps its "Road"."""
    return any(ch.isdigit() for ch in token) or bool(
        re.fullmatch(r"[^\W\d_][.,:]?", token))


def _street_part(part: str) -> str:
    """One part of an address with its numbers, units and postcodes removed,
    or "" when nothing that names a place is left."""
    toks = []
    for tok in part.split():
        if _HAS_CJK.search(tok):
            tok = _CJK_NUMBER.sub("", tok)
        if tok:
            toks.append(tok)
    n = len(toks)
    low = [t.lower().strip(".:") or t for t in toks]
    drop = [False] * n
    i = 0
    while i < n:
        nxt = toks[i + 1] if i + 1 < n else ""
        if low[i] in ("po", "p.o") and i + 1 < n and low[i + 1] == "box":
            drop[i] = True
        elif (_ORDINAL.match(toks[i]) and i + 1 < n
              and low[i + 1] in _FLOOR_WORDS):            # "3rd Floor"
            drop[i] = drop[i + 1] = True
            i += 1
        elif nxt and (
                (low[i] in _UNIT_WORDS and _is_identifier(nxt))
                or (low[i] in _BLOCK_WORDS
                    and (_is_identifier(nxt)
                         or re.fullmatch(r"[^\W_]{1,3}[.,]?", nxt)))
                or (low[i] in _LANE_WORDS and _HOUSE_NUMBER.match(nxt)
                    and i + 2 < n)):
            drop[i] = drop[i + 1] = True
            i += 1
        i += 1
    kept = [j for j in range(n) if not drop[j]]
    num = {j for j in kept
           if _HOUSE_NUMBER.match(toks[j]) and not _ORDINAL.match(toks[j])}
    for k, j in enumerate(kept):
        if j in num:
            continue
        before = kept[k - 1] if k > 0 else None
        after = kept[k + 1] if k + 1 < len(kept) else None
        if before in num and (
                (low[j] in _NUMBER_JOINERS and after in num)
                or low[j] in _NUMBER_SUFFIXES
                or (len(low[j]) == 1 and low[j].isalpha()
                    and low[j] not in _DIRECTIONALS)):      # "12 A Main St"
            num.add(j)
    # A route whose whole name is a road word keeps its number: "Calle 31",
    # "20544 HIGHWAY 370" -> "HIGHWAY 370". Only a LEADING run of house
    # numbers is skipped; everything between it and the last token must match
    # _ROUTE_NAME whole, so a number in the middle fails the match and none is
    # kept ("Calle 31 12").
    route = None
    if kept and _ROUTE_NUMBER.match(toks[kept[-1]]):
        lead = 0
        while lead < len(kept) - 1 and kept[lead] in num:
            lead += 1
        rest = kept[lead:-1]
        if rest and _ROUTE_NAME.match(" ".join(toks[j] for j in rest)):
            route = kept[-1]
            num.discard(route)
    # A house number between two words ends the street: "Hauptstrasse 5
    # 10115 Berlin" -> "Hauptstrasse", when what precedes it names a street.
    words = [j for j in kept if j not in num]
    for k, j in enumerate(kept):
        if j in num and 0 < k < len(kept) - 1:
            head = [x for x in kept[:k] if x not in num]
            if head and any(x not in num for x in kept[k + 1:]) and \
                    _names_a_street(" ".join(toks[x] for x in head)):
                words = head
                break
    out = [toks[j] for j in words
           if j == route or _ORDINAL.match(toks[j])
           or not any(ch.isdigit() for ch in toks[j])]
    while out and (out[0].lower() in _EDGE_JUNK
                   or not any(ch.isalnum() for ch in out[0])):
        out.pop(0)
    while out and (out[-1].lower() in _EDGE_JUNK
                   or not any(ch.isalnum() for ch in out[-1])):
        out.pop()
    if not out:
        return ""
    text = " ".join(out)
    named = (route is not None or _HAS_CJK.search(text)
             or any(_ORDINAL.match(t) for t in out)
             or any(w.lower() not in _NOT_A_NAME for w in _WORDS.findall(text)))
    return text if named else ""


def street_name_only(address) -> str:
    """The street NAME in `address`, without house or building numbers, units,
    plots or postcodes; "" when nothing but those is there.

        "2500 MAPLE RD"                    -> "MAPLE RD"
        "Unit 5, 3 Mill Lane"              -> "Mill Lane"
        "Rue des Lilas 12"                 -> "Rue des Lilas"
        "Plot 7"                           -> ""
        "Springfield"                      -> "Springfield"   (not validated)

    Picks the first comma-separated part that names a street, else the first
    part with anything left in it."""
    v = _clean(address)
    if not v:
        return ""
    v = _GLUED_MARK.sub(r"\1 ", _GLUED_ABBREV.sub(". ", v))
    names = [s for s in (_street_part(p) for p in _ADDRESS_PARTS.split(v))
             if s]
    for s in names:
        if _names_a_street(s):
            return s
    return names[0] if names else ""


# ── recorded changes ────────────────────────────────────────────────────────

# The routes/temporal_capture layer that tracks the rows these pages render,
# and the tracked fields a reader can see on the page, with the label each
# gets. The layer's other fields (state, market, is_duplicate, merged_at) are
# restated elsewhere or are bookkeeping, and produce no line.
CHANGE_LAYER = "discovered_facilities"
CHANGE_FIELDS = {
    "status": "Status",
    "provider": "Operator",
    "power_mw": "Reported capacity",
    "name": "Name",
    "city": "City",
}
CHANGE_LIST_MAX = 5


def _change_value(field: str, raw) -> str:
    """One stored old/new value as the page may print it, or "" when it is not
    a real value — the same predicates the page's own facts use."""
    if field == "power_mw":
        return display_mw(raw)
    if field == "provider":
        return real_operator(raw)
    if field == "status":
        return real_status(raw)
    v = _clean(raw)
    if field == "city":
        return "" if is_placeholder_city(v) else v
    return "" if v.lower() in PLACEHOLDER_VALUES else v


def _utc_day(at):
    if isinstance(at, datetime.datetime):
        if at.tzinfo is not None:
            at = at.astimezone(datetime.timezone.utc)
        return at.date()
    if isinstance(at, datetime.date):
        return at
    return None


def display_day(day) -> str:
    """"Sep 14, 2026" """
    return f"{day:%b} {day.day}, {day.year}"


def change_items(rows, limit: int = CHANGE_LIST_MAX):
    """[(date, text)], newest first, at most `limit`, from entity_changes rows
    shaped (kind, field, old_value, new_value, detected_at).

    A change the page cannot state with real values produces NO line — a value
    becoming a placeholder, a capacity plausible_mw refuses, a case-only edit —
    so "Last updated" (the first item's date) is always the date of a change
    the reader can see."""
    items = []
    for row in rows or ():
        try:
            kind, field, old, new, at = row[:5]
        except (TypeError, ValueError):
            continue
        day = _utc_day(at)
        if day is None:
            continue
        if kind == "appeared":
            text = "Added to DC Hub"
        elif kind == "field_change" and field in CHANGE_FIELDS:
            now = _change_value(field, new)
            before = _change_value(field, old)
            if not now or before.lower() == now.lower():
                continue
            label = CHANGE_FIELDS[field]
            text = (f"{label} changed from {before} to {now}" if before
                    else f"{label} set to {now}")
        else:
            continue
        items.append((day, text))
    items.sort(key=lambda it: it[0], reverse=True)
    return items[:max(0, limit)]
