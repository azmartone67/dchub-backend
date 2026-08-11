"""util/country_codes.py — one canonical country code per country (2026-08-11).

`UK` is not a country code. ISO 3166-1 alpha-2 assigns **GB** to the United
Kingdom; `UK` is only "exceptionally reserved". We had 759 facilities under GB
and 6 under UK, so a caller who sent the code our own OpenAPI spec documents
("ISO 3166-1 alpha-2 country code") silently missed those 6 rows.

That failure mode is quieter than a wrong label and worse in one way: a wrong
country is visible and gets reported, while a split code just returns an
incomplete answer that looks complete.

Deliberately NOT a full ISO table. This maps only the aliases we have actually
observed in ingested data, so an unknown value passes through unchanged rather
than being silently coerced into something wrong. Adding an entry is a one-line
change backed by a row we saw.

See [[reference_dchub_identity_joins_index]] for the wider id-space rules.
"""
from __future__ import annotations

# alias -> canonical ISO 3166-1 alpha-2.
# Keys are compared upper-cased and stripped.
_ALIASES = {
    "UK": "GB",       # United Kingdom — ISO is GB; UK is exceptionally reserved
    "GBR": "GB",      # alpha-3 leaking in from a feed
    "USA": "US",
    "UNITED KINGDOM": "GB",
    "UNITED STATES": "US",
    "ENGLAND": "GB",  # a country of the UK, not a country code
    "SCOTLAND": "GB",
    "WALES": "GB",
    "NETHERLANDS": "NL",
    "GERMANY": "DE",
    "FRANCE": "FR",
    "DEU": "DE",
    "NLD": "NL",
    "FRA": "FR",
    "AUS": "AU",
    "JPN": "JP",
    "CAN": "CA",
    "SGP": "SG",
}


def canon_country(raw):
    """Canonical alpha-2 code, or None when nothing was supplied.

    Returns None for absent input — never a default. Guessing a country is the
    exact defect this module exists alongside: see the 'US' fallbacks removed
    from every discovery writer on 2026-08-11.

    An unrecognised value is returned upper-cased and trimmed rather than
    dropped, so a genuine code we have not enumerated still works and a junk
    value stays visible for review instead of vanishing.
    """
    if raw is None:
        return None
    try:
        s = str(raw).strip()
    except Exception:
        return None
    if not s:
        return None
    key = s.upper()
    if key in _ALIASES:
        return _ALIASES[key]
    # Already a plausible alpha-2 code.
    if len(key) == 2 and key.isalpha():
        return key
    # Longer free text we do not recognise: keep it, upper-cased, for review.
    return key


def is_alpha2(code):
    """True when the value is shaped like an ISO alpha-2 code."""
    try:
        s = str(code or "").strip()
    except Exception:
        return False
    return len(s) == 2 and s.isalpha() and s == s.upper()
