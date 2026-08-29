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


def country_filter(raw):
    """Read-path rule for a caller-supplied `country` FILTER.

    Returns `(code, error)`; at most one is ever non-None.

      (None, None)   nothing supplied — do not filter
      ("US", None)   usable alpha-2, upper-cased
      (None, "...")  not an alpha-2 code; `error` says so in words

    Why this is not just `canon_country()`. That function is the WRITE-path
    rule: it maps observed aliases so ingested rows land under one code, and
    "UNITED STATES" is in that table. Applying it to a caller's filter would
    silently answer a different question than the one asked — the caller sent
    a value our own schema says is invalid and would get rows back as if it
    were fine, learning nothing. On the read path an unusable filter must be
    NAMED, because the alternative is `success: true, data: []` — a confident
    empty that reads as "DC Hub has no US facilities". Same failure shape as
    util/state_codes.py (?state=TX -> [] because the column held FIPS).

    Case IS folded: `us` and `US` are the same code, and rejecting `us` would
    turn a second silent-empty into a second wrong answer. Folding case is
    lossless; mapping a name to a code is a guess. Only the first is done here.

    `canon_country` still supplies the SUGGESTION when it knows one, so the
    message can say "try US" — sourced from the single table, never applied.
    """
    if raw is None:
        return None, None
    try:
        s = str(raw).strip()
    except Exception:
        s = ""
    if not s:
        return None, None

    if len(s) == 2 and s.isalpha():
        return s.upper(), None

    hint = canon_country(s)
    suggestion = ""
    if hint and is_alpha2(hint) and hint != s.upper():
        suggestion = " — try %s" % hint
    return None, (
        "country expects an ISO 3166-1 alpha-2 code (e.g. US, GB, SG); "
        "%r is not one%s" % (s, suggestion)
    )
