"""util/state_codes.py — one place that turns a state token into a FIPS code
(2026-08-13).

`/api/v1/fiber/providers?state=TX` returned `{"ok": true, "providers": []}` —
success, empty list — while `?state=48` returned 100 providers. The column holds
FIPS codes and the parameter was passed through raw, so every human-shaped input
silently matched nothing.

That is the worst failure shape available: not an error a caller can handle, but
a confident wrong answer. A consumer reads "no fiber providers in Texas" and has
no way to tell that from "you used the wrong format". Anyone reaching for this
endpoint types TX, not 48.

Covers the 50 states + DC (11) + Puerto Rico (72) — matching `_ALL_STATE_FIPS`
in routes/fcc_bdc_fiber.py, which is what the FCC BDC loader actually populates.

★There were already FOUR separate state->FIPS maps in this repo when this was
written (water_drought_routes.py, water_drought_intel.py,
scripts/fiber_connectivity_discovery.py, and a FIPS-only list in
routes/fcc_bdc_fiber.py). This is the shared one; new callers should use it
rather than adding a fifth. The existing three are deliberately NOT refactored
here — that is a separate change with its own blast radius.
"""
from __future__ import annotations

# USPS abbreviation -> 2-digit FIPS.
_ABBR_FIPS = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08",
    "CT": "09", "DE": "10", "DC": "11", "FL": "12", "GA": "13", "HI": "15",
    "ID": "16", "IL": "17", "IN": "18", "IA": "19", "KS": "20", "KY": "21",
    "LA": "22", "ME": "23", "MD": "24", "MA": "25", "MI": "26", "MN": "27",
    "MS": "28", "MO": "29", "MT": "30", "NE": "31", "NV": "32", "NH": "33",
    "NJ": "34", "NM": "35", "NY": "36", "NC": "37", "ND": "38", "OH": "39",
    "OK": "40", "OR": "41", "PA": "42", "RI": "44", "SC": "45", "SD": "46",
    "TN": "47", "TX": "48", "UT": "49", "VT": "50", "VA": "51", "WA": "53",
    "WV": "54", "WI": "55", "WY": "56", "PR": "72",
}

# Full name -> FIPS, so ?state=Texas works too.
_NAME_FIPS = {
    "ALABAMA": "01", "ALASKA": "02", "ARIZONA": "04", "ARKANSAS": "05",
    "CALIFORNIA": "06", "COLORADO": "08", "CONNECTICUT": "09",
    "DELAWARE": "10", "DISTRICT OF COLUMBIA": "11", "FLORIDA": "12",
    "GEORGIA": "13", "HAWAII": "15", "IDAHO": "16", "ILLINOIS": "17",
    "INDIANA": "18", "IOWA": "19", "KANSAS": "20", "KENTUCKY": "21",
    "LOUISIANA": "22", "MAINE": "23", "MARYLAND": "24", "MASSACHUSETTS": "25",
    "MICHIGAN": "26", "MINNESOTA": "27", "MISSISSIPPI": "28", "MISSOURI": "29",
    "MONTANA": "30", "NEBRASKA": "31", "NEVADA": "32", "NEW HAMPSHIRE": "33",
    "NEW JERSEY": "34", "NEW MEXICO": "35", "NEW YORK": "36",
    "NORTH CAROLINA": "37", "NORTH DAKOTA": "38", "OHIO": "39",
    "OKLAHOMA": "40", "OREGON": "41", "PENNSYLVANIA": "42",
    "RHODE ISLAND": "44", "SOUTH CAROLINA": "45", "SOUTH DAKOTA": "46",
    "TENNESSEE": "47", "TEXAS": "48", "UTAH": "49", "VERMONT": "50",
    "VIRGINIA": "51", "WASHINGTON": "53", "WEST VIRGINIA": "54",
    "WISCONSIN": "55", "WYOMING": "56", "PUERTO RICO": "72",
}

VALID_FIPS = frozenset(_ABBR_FIPS.values())


def to_fips(token):
    """Return the 2-digit FIPS string for a state token, or None.

    Accepts a USPS abbreviation ("TX", "tx"), a FIPS code ("48", 48, "6"), or a
    full name ("Texas", "new york"). Returns None when the token cannot be
    resolved — ★None means UNRECOGNISED, and the caller must not treat that as
    "no rows". Conflating the two is the defect this module exists to stop.
    """
    if token is None:
        return None
    try:
        s = str(token).strip()
    except Exception:
        return None
    if not s:
        return None

    up = s.upper()
    if up in _ABBR_FIPS:
        return _ABBR_FIPS[up]
    if up in _NAME_FIPS:
        return _NAME_FIPS[up]

    # Numeric: accept "6" and 6 as well as "06". Zero-pad to two digits.
    if s.isdigit():
        padded = s.zfill(2)
        if padded in VALID_FIPS:
            return padded
        return None

    return None
