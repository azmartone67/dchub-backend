"""Sponsor brand-mention detection over observed AI answers (P2-4, 2026-08-28).

WHY THIS EXISTS. /advertise prices Product 2 at $3,000 rotating and $5,000
category-exclusive. The entire premium of the exclusive tier is a report saying
"your brand appeared in N AI answers" — and no detector existed. `response_text`
is stored on every ai_citations row and read in ~94 places; not one of them
looks for a sponsor's name. Quoting $5,000 before this module existed sold a
report we could not produce.

WHAT IT IS HONEST ABOUT, because an agency will ask all four:

1. THE DENOMINATOR IS OURS. These are answers WE elicited by probing a fixed
   prompt set. "Your brand appeared in 12 AI answers" invites the reader to
   imagine the open internet. Every number here ships with the sample it came
   from, and the caller is expected to print both.

2. `response_text` IS TRUNCATED AT 2000 CHARS. Measured 2026-08-28: mean 643,
   max exactly 2000 across 1,827 rows — a ceiling, not a coincidence. A brand
   named only in a long answer's tail is invisible to this scan. That
   UNDER-counts, which is the safe direction for an invoice, and it is
   disclosed rather than fixed by loosening the match.

3. TWO COLLECTORS LABEL THE SAME VENDOR DIFFERENTLY. ai_citation_scraper.py
   writes engine='gpt'; ai_citation_tracker.py writes engine='chatgpt'. They
   are one engine. Reporting them as two would let a sponsor read one vendor as
   two independent confirmations.

4. A SHORT OR COMMON BRAND NAME OVER-MATCHES. Word-boundary matching stops
   "Vantage" inside "advantage", but nothing stops a brand that IS a common
   word ("Core", "Switch", "Equinix" is fine, "Switch" is not). Those brands
   come back flagged, with samples, rather than with a confident integer.

MATCHING. Postgres `~*` with \\m...\\M word boundaries, not LIKE '%brand%'.
LIKE cannot express a word boundary, so it counts "advantage" as "Vantage". The
table is ~1.8k rows, so a sequential regex scan is cheap; correctness wins here
and there is no index to preserve.
"""
import logging
import os
import re

logger = logging.getLogger(__name__)

# The vendor behind each stored label. See point 3 above.
_ENGINE_CANON = {
    "gpt": "openai", "chatgpt": "openai",
    "claude": "anthropic",
    "perplexity": "perplexity",
    "gemini": "google",
    "groq": "groq",
}

# Brands whose name is an ordinary English word cannot be counted confidently
# from a substring match alone. Not exhaustive — the length check below catches
# the rest, and both only ever RAISE a flag, never drop a row.
# ★ "stack", "aligned" and "compass" are in this list because a real scan
#   found them, not because they looked risky. Over the 30 days to 2026-08-28,
#   "STACK" matched 3 answers that also cited DC Hub — every one of them the
#   English word ("Stack Exchange", "technology stack", "software stack"), none
#   referring to STACK Infrastructure. Reported unflagged, that is a sponsor
#   invoiced for three citations that do not exist.
_COMMON_WORDS = {
    "core", "switch", "vantage", "digital", "prime", "edge", "flex", "nova",
    "summit", "beacon", "atlas", "apex", "vertical", "element", "align",
    "stack", "aligned", "compass",
}

_RESPONSE_TEXT_CEILING = 2000   # observed max length; see point 2


def _canon_engine(raw) -> str:
    v = (raw or "").strip().lower()
    return _ENGINE_CANON.get(v, v or "unknown")


def _brand_terms(brand: str, aliases=()) -> list:
    """The brand plus any aliases, cleaned. Empty list means: do not scan."""
    out = []
    for t in [brand, *(aliases or [])]:
        t = (t or "").strip()
        if len(t) >= 2:
            out.append(t)
    return out


def _regex_for(term: str) -> str:
    r"""A word-boundary regex for `term`, with every metacharacter escaped.

    re.escape, then \m...\M. Without the escape a brand containing '.' or '+'
    ("A+ Data", "dc.one") silently becomes a wildcard and over-counts.
    """
    return r"\m" + re.escape(term) + r"\M"


def _ambiguity_flags(terms) -> list:
    flags = []
    for t in terms:
        low = t.strip().lower()
        if low in _COMMON_WORDS:
            flags.append(f"{t!r} is an ordinary English word; matches may not "
                         f"refer to the sponsor")
        elif len(low) <= 3:
            flags.append(f"{t!r} is {len(low)} characters; too short to attribute "
                         f"confidently")
    return flags


def brand_mentions(brand, aliases=(), days=30, conn=None, samples=5) -> dict:
    """How often `brand` appears in the AI answers we observed.

    Returns the count AND the sample it came from. A caller that prints the
    numerator without the denominator is misreporting, so both are required
    fields and `limits` is never empty.
    """
    terms = _brand_terms(brand, aliases)
    base = {
        "brand": brand, "aliases": list(aliases or []), "window_days": int(days),
        "sampled_answers": 0, "mentions": 0, "by_engine": {},
        "alongside_dchub": 0, "prior_window": None, "samples": [],
        "ambiguous": [], "limits": [], "ok": False,
    }
    if not terms:
        base["limits"].append("no usable brand term supplied")
        return base

    base["ambiguous"] = _ambiguity_flags(terms)
    owned = conn is None
    if owned:
        try:
            from main import get_read_db
            conn = get_read_db()
        except Exception as e:
            base["limits"].append(f"database unavailable: {e}")
            return base
    if conn is None:
        base["limits"].append("database unavailable")
        return base

    # ONE regex alternation for all terms, so a row naming two aliases counts
    # once. Counting per-term and summing would double-count it.
    pattern = "|".join(_regex_for(t) for t in terms)
    # ai_citations carries both a tz-aware observed_at and a naive detected_at;
    # rows exist with only one, so neither alone is a usable clock.
    stamp = "COALESCE(observed_at, detected_at AT TIME ZONE 'UTC')"
    window = f"{stamp} > now() - (%s || ' days')::interval"
    prior = (f"{stamp} <= now() - (%s || ' days')::interval AND "
             f"{stamp} > now() - (2 * (%s || ' days')::interval)")
    usable = "response_text IS NOT NULL AND length(response_text) > 40"

    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT count(*), "
                f"       count(*) FILTER (WHERE response_text ~* %s), "
                f"       count(*) FILTER (WHERE response_text ~* %s "
                f"                          AND COALESCE(dchub_cited, false)) "
                f"  FROM ai_citations WHERE {usable} AND {window}",
                (pattern, pattern, str(int(days))),
            )
            r = cur.fetchone() or (0, 0, 0)
            base["sampled_answers"] = int(r[0] or 0)
            base["mentions"] = int(r[1] or 0)
            base["alongside_dchub"] = int(r[2] or 0)

            cur.execute(
                f"SELECT COALESCE(engine, platform, 'unknown'), count(*) "
                f"  FROM ai_citations "
                f" WHERE {usable} AND {window} AND response_text ~* %s "
                f" GROUP BY 1",
                (str(int(days)), pattern),
            )
            rolled = {}
            for raw, n in (cur.fetchall() or []):
                k = _canon_engine(raw)
                rolled[k] = rolled.get(k, 0) + int(n or 0)
            base["by_engine"] = rolled

            # The same window immediately before this one. A bare count is
            # unreadable without it: 12 is good news or bad depending only on
            # what it was last month.
            cur.execute(
                f"SELECT count(*), count(*) FILTER (WHERE response_text ~* %s) "
                f"  FROM ai_citations WHERE {usable} AND {prior}",
                (pattern, str(int(days)), str(int(days))),
            )
            pr = cur.fetchone() or (0, 0)
            base["prior_window"] = {"sampled_answers": int(pr[0] or 0),
                                    "mentions": int(pr[1] or 0)}

            if samples:
                cur.execute(
                    f"SELECT COALESCE(engine, platform, 'unknown'), "
                    f"       COALESCE(prompt_text, query, ''), "
                    f"       substring(response_text from 1 for 400), {stamp} "
                    f"  FROM ai_citations "
                    f" WHERE {usable} AND {window} AND response_text ~* %s "
                    f" ORDER BY {stamp} DESC LIMIT %s",
                    (str(int(days)), pattern, int(samples)),
                )
                base["samples"] = [
                    {"engine": _canon_engine(s[0]), "query": s[1],
                     "excerpt": s[2], "observed_at": str(s[3])}
                    for s in (cur.fetchall() or [])
                ]
        base["ok"] = True
    except Exception as e:
        logger.warning("[sponsor_mentions] scan failed for %r: %s", brand, e)
        base["limits"].append(f"scan failed: {e}")
        return base
    finally:
        if owned:
            try: conn.close()
            except Exception: pass

    # ★ Always populated. A report that prints a count with no limits section
    #   is the thing this module exists not to produce.
    base["limits"].extend([
        f"Counted over {base['sampled_answers']} AI answers DC Hub elicited in "
        f"the last {int(days)} days by probing a fixed prompt set. This is a "
        f"sample of our own making, not a measurement of the open internet.",
        f"Stored answers are truncated at {_RESPONSE_TEXT_CEILING} characters, "
        f"so a brand named only in a longer answer's tail is not counted. This "
        f"under-counts.",
        "engine='gpt' and engine='chatgpt' are two collectors writing the same "
        "vendor; they are rolled up as one.",
    ])
    if base["ambiguous"]:
        base["limits"].append(
            "Brand term is ambiguous — read the samples before quoting the count.")
    return base
