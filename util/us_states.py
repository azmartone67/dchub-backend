"""us_states.py — USPS abbreviation <-> full state name, and the one helper
for querying a table that stores states in the OTHER spelling.

★ WHY THIS EXISTS. `eia_retail_rates.state` holds FULL names ("Virginia"),
while every caller passes the 2-letter code ("VA"). A query written as

    WHERE UPPER(state) = 'VA'

runs clean, returns zero rows, and the caller publishes null — a silent
absence that looks exactly like "this state has no data". Measured live
2026-09-21: `UPPER(state) = 'VA'` -> 0 rows; `'VIRGINIA'` -> 10 rows, the
newest being industrial 10.09 c/kWh for period 2026. The same table also
carries census-region rows ("East North Central"), so it is genuinely a
mixed-vocabulary column and matching both spellings is the only safe read.

★ WHY A SHARED MODULE. routes/site_brief.py carried this map as a private
`_STATE_FULL` with a comment describing the identical bug, and eight more
route modules carry their own copy. util/deals.py records where that ends:
seven hand-written copies of one predicate in two spellings, and a census
that could not check any of them because they were function-local. This is
the importable home so the tenth copy does not get written; migrating the
existing nine is a separate change.
"""

from __future__ import annotations

__all__ = ["ABBR_TO_NAME", "NAME_TO_ABBR", "full_name", "state_match_pair"]

ABBR_TO_NAME = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut",
    "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida",
    "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky",
    "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
}

#: Upper-cased full name -> USPS code. Built from the map above so the two
#: can never disagree.
NAME_TO_ABBR = {name.upper(): abbr for abbr, name in ABBR_TO_NAME.items()}


def full_name(state: str) -> str:
    """The full name for a USPS code. A value that is already a full name (or
    is unknown) comes back unchanged, so this is safe to call on either
    spelling."""
    s = (state or "").strip()
    return ABBR_TO_NAME.get(s.upper(), s)


def state_match_pair(state: str) -> tuple[str, str]:
    """`(CODE, FULL NAME)` both upper-cased, for `WHERE UPPER(state) IN (%s, %s)`.

    Accepts either spelling. Both elements are always returned — never None —
    so the caller's SQL keeps a fixed parameter count. When the input is
    neither a known code nor a known name (a census region, a typo), both
    elements are the input itself, which matches exactly what was asked for
    and nothing else.
    """
    s = (state or "").strip().upper()
    if s in ABBR_TO_NAME:
        return s, ABBR_TO_NAME[s].upper()
    if s in NAME_TO_ABBR:
        return NAME_TO_ABBR[s], s
    return s, s
