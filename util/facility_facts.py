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
