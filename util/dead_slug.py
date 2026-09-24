"""Dead-slug guard: a null serialised as text is not a slug.

r-facility-dead-slug (2026-09-24). meta-externalagent re-crawled
https://dchub.cloud/facilities/null ~60 times in 42h (Railway http log,
2026-09-22T13:09Z..2026-09-24T06:43Z) and also /facilities/None and
/facilities/undefined. Each is what `/facilities/{slug}` renders when the
slug is missing, in one language or another:

    JS      `/facilities/${null}`       -> /facilities/null
    JS      `/facilities/${undefined}`  -> /facilities/undefined
    Python  f"/facilities/{None}"       -> /facilities/None
    JSON    template "{slug}" filled from a record with no slug

A `if slug:` truthiness guard catches the first three only while the value is
still a real None. Once it has been written to a column, a cache or a JSON
payload as the TEXT "null", it is truthy and sails through every guard in
this codebase. is_dead_slug() is the one check for both shapes.

Exact tokens only: a real slug that merely CONTAINS one of these words
("unknown-null-networks-1a2b3c4d") is live.
"""

DEAD_SLUG_TOKENS = frozenset({"", "null", "none", "undefined", "nan"})


def is_dead_slug(value):
    """True when `value` cannot be a /facilities/<slug> segment: None, or text
    that is blank / null / None / undefined / nan after trimming, case-folding
    and dropping a trailing .html/.json. NEVER raises."""
    try:
        if value is None:
            return True
        s = str(value).strip().lower()
        for ext in (".html", ".json"):
            if s.endswith(ext):
                s = s[: -len(ext)].strip()
        return s in DEAD_SLUG_TOKENS
    except Exception:
        return True


def live_slug(value):
    """The trimmed slug text, or None when is_dead_slug(value). NEVER raises."""
    if is_dead_slug(value):
        return None
    try:
        return str(value).strip()
    except Exception:
        return None
