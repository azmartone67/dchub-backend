"""deal_corroboration.py — may an auto-ingested headline be published as a deal?

One predicate, two consumers, the same stored inputs:

  * deal_scraper.article_to_deal (the WRITER) reads buyer, seller and type out
    of the headline with this module and refuses a row it cannot corroborate;
  * the quarantine sweep over rows written before that fix asks the same
    question of what is already stored: deals.notes (the scraper stores the
    headline there), buyer, seller, type.

WHY (measured 2026-09-21 on prod `deals`)
-----------------------------------------
829 published AUTO-* rows, every one with an empty source_url. deal_scraper's
"X acquires Y" regex had been corrupted (`?` rewritten to `%s`, so it demanded
a literal "%s" and never matched), and every article fell through to a
fallback that set buyer = first and seller = second KNOWN company found
anywhere in title + summary — in LIST order, by bare substring. So
"Nvidia Buying an Additional $1.5 Billion in SB Energy Shares" became
Ares (sh-ARES) acquires Nvidia, and "Anthropic and Microsoft Dominate
Nscale's $103 Billion in Contracts" became Microsoft acquires Anthropic,
typed M&A because parse_deal_type fell back to M&A when it recognised nothing.

THE RULE
--------
A row is publishable only when its headline states it:
  1. the buyer is named as whole words ("Ares" is not in "Shares");
  2. the headline names the row's deal type (whole words: "Released" is not
     a lease, "emerging" is not a merger);
  3. a seller, when stored, is linked to the buyer by a directional verb —
     "<buyer> acquires|buys|to acquire|invests in <seller>", or the passive
     "<seller> acquired by|sold to <buyer>". Two names in one headline are
     not a transaction;
  4. no seller -> not M&A (an acquisition has a target).

stdlib only, no I/O: importable from a cron, a route and a test alike.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

__all__ = ["UNCORROBORATED_FLAG", "directional_pairs", "extract_directional_pair",
           "is_corroborated", "names_whole_word", "stated_deal_type"]

#: data_flag for rows this predicate rejects. util.deals.DEALS_OK hides the
#: `quarantine_` prefix from every guarded published read; the row is kept.
UNCORROBORATED_FLAG = "quarantine_uncorroborated"

# Capitalised tokens that end a company name rather than belong to one.
_NOT_NAME = (
    r"(?:Shares?|Stakes?|Ahead|For|In|Into|To|With|From|After|Amid|As|At|On|Over|"
    r"By|Via|Deals?|Billion|Million|Bn|Assets?|Portfolio|Business|Unit|Firm|"
    r"Startup|Stock|Bid|Offer|Talks|Report|Says|Said|And|Or|The|An?|Its|Their|"
    r"Data|Cent(?:er|re)s?|Campus(?:es)?|Sites?|Plans?|Seeks|Eyes|Nears?|Could|"
    r"May|Will|Would|Is|Are|Was|Be|Why|How|What|AI|Minority|Majority|Controlling|"
    r"Additional|Stake)\b"
)
# Descriptors a headline puts in front of the target — "Stake In Hyperscale
# Data Center Developer AREP", "London Data Centre Volta" — skipped, not named.
# Generic nouns can never be part of a name; places can ("Dallas Infrastructure").
_GENERIC = (
    r"(?:Hyperscale|Hyperscaler|Data|Cent(?:er|re)s?|Developer|Operator|Provider|"
    r"Colocation|Colo|Cloud|Chip|Chipmaker|Startup|Firm|Company|Platform|Portfolio|"
    r"Pan-European|European|American|Asian|British|Leading|Highly|Interconnected)\b"
)
_DESCRIPTOR = (
    rf"(?:{_GENERIC}|(?:London|Frankfurt|Amsterdam|Paris|Dublin|Singapore|Tokyo|"
    r"Sydney|Virginia|Texas|Phoenix|Dallas|Chicago|Ohio|Atlanta)\b)"
)
_TOK = rf"(?!{_NOT_NAME})(?!{_GENERIC})(?:[A-Z][\w.&'’\-]*|\d+[A-Za-z][\w.&'\-]*)"
_ENT = rf"{_TOK}(?:\s+(?:&\s+|of\s+)?{_TOK}){{0,4}}"

# Between the verb and the target: "buys a 20% stake in", "buying an
# additional $1.5 billion in".
_FILLER = (r"(?i:(?:an?\s+)?(?:additional\s+)?(?:majority\s+|minority\s+|controlling\s+)?"
           r"(?:\$?[\d.,]+\s*(?:%|billion|million|bn|mn|[bm])?\s+)?"
           r"(?:stake\s+|interest\s+|shares?\s+)?(?:in\s+|of\s+))?")

_ACTIVE_VERB = (
    r"(?i:(?:has\s+|have\s+)?(?:agree[sd]?\s+to\s+|to\s+|will\s+|(?:is\s+)?set\s+to\s+|"
    r"moves?\s+to\s+|plans?\s+to\s+|seeks?\s+to\s+|looks?\s+to\s+|in\s+talks\s+to\s+|"
    r"nears?\s+(?:deal\s+to\s+)?)?"
    r"(?:acquires?|acquired|acquiring|buys?|bought|buying|purchases?|purchased|"
    r"takes?\s+over|took\s+over|merges?\s+with|merged\s+with|snaps?\s+up|snapped\s+up|"
    r"invest(?:s|ed|ing)?(?:\s+in)?|backs|backed|"
    r"(?:completes?|completed|closes?|closed)\s+(?:the\s+|its\s+)?"
    r"(?:acquisition|purchase|takeover)\s+of))"
)
_PASSIVE_VERB = (
    r"(?i:(?:to\s+be\s+|is\s+being\s+|is\s+|was\s+)?(?:acquired|bought|purchased|taken\s+over)\s+by|"
    r"(?:sells?|sold|to\s+sell|agree[sd]?\s+to\s+sell)\s+(?:.{0,60}?\s+)?to)"
)
_ACTIVE_RE = re.compile(rf"(?<![\w&])({_ENT})\s+{_ACTIVE_VERB}\s+{_FILLER}"
                        rf"(?:{_DESCRIPTOR},?\s+){{0,5}}({_ENT})")
_PASSIVE_RE = re.compile(rf"(?<![\w&])({_ENT})\s+{_PASSIVE_VERB}\s+({_ENT})")

# Deal-type signals, whole words only, in the order a type is assigned. The
# names match what deals.type already holds.
_TYPE_SIGNALS = (
    ("M&A", r"acquir(?:e|es|ed|ing)|acquisitions?|takeovers?|takes?\s+over|took\s+over|"
            r"mergers?|merg(?:e|es|ed|ing)|buys?|bought|buying|purchas(?:e|es|ed|ing)|"
            r"sells?|sold|to\s+sell"),
    ("JV", r"joint\s+ventures?|jv|partnerships?"),
    ("Equity", r"equity|funding|rais(?:e|es|ed|ing)|investment\s+round|series\s+[a-h]|"
               r"invest(?:s|ed|ing)?|stakes?|shares"),
    ("Debt", r"debt|loans?|financing|credit\s+facility"),
    ("IPO", r"ipo|goes\s+public|public\s+offering|listed"),
    ("Power Agreement", r"power\s+agreements?|ppas?|power\s+purchase|energy\s+deal"),
    ("New Build", r"land|campus|breaks\s+ground|new\s+build|construction"),
    ("Lease", r"lease[sd]?|leasing"),
    ("CapEx", r"capex|capital\s+expenditure|spending"),
)
_TYPE_RE = {name: re.compile(rf"(?<![a-z])(?:{pat})(?![a-z])") for name, pat in _TYPE_SIGNALS}
_STAKE_RE = re.compile(r"(?<![a-z])(?:stakes?|shares)(?![a-z])")


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def names_whole_word(name: str, text: str) -> bool:
    """True when `name` occurs in `text` as whole words."""
    n = _norm(name)
    return bool(n) and re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])",
                                 _norm(text)) is not None


def stated_deal_type(headline: str) -> Optional[str]:
    """The deal type the headline names, or None when it names none."""
    text = (headline or "").lower()
    for name, _ in _TYPE_SIGNALS:
        if _TYPE_RE[name].search(text):
            # Buying shares of / a stake in a company is an equity deal.
            if name == "M&A" and _STAKE_RE.search(text):
                return "Equity"
            return name
    return None


def _type_stated(deal_type: Optional[str], headline: str) -> bool:
    key = {n.lower(): n for n, _ in _TYPE_SIGNALS}.get((deal_type or "").strip().lower())
    return bool(key) and _TYPE_RE[key].search((headline or "").lower()) is not None


def _clean(phrase: str) -> str:
    # A possessive ends the party: "PhoenixNAP's Phoenix Data Center" -> PhoenixNAP.
    return re.split(r"['’]s\b", phrase)[0].strip(" ,:;.")


def directional_pairs(headline: str) -> List[Tuple[str, str]]:
    """Every (buyer, seller) the headline states with a directional verb."""
    text = headline or ""
    pairs = [(_clean(m.group(1)), _clean(m.group(2))) for m in _ACTIVE_RE.finditer(text)]
    pairs += [(_clean(m.group(2)), _clean(m.group(1))) for m in _PASSIVE_RE.finditer(text)]
    return [(b, s) for b, s in pairs if len(b) >= 2 and len(s) >= 2 and _norm(b) != _norm(s)]


def extract_directional_pair(headline: str) -> Tuple[Optional[str], Optional[str]]:
    """The first directional (buyer, seller) in the headline, else (None, None)."""
    pairs = directional_pairs(headline)
    return pairs[0] if pairs else (None, None)


def _same_party(stored: str, stated: str) -> bool:
    # "Blackstone" stored vs "Blackstone Infrastructure" stated, or the reverse.
    return names_whole_word(stored, stated) or names_whole_word(stated, stored)


def is_corroborated(headline: str, buyer: Optional[str], seller: Optional[str],
                    deal_type: Optional[str]) -> bool:
    """May this (buyer, seller, type) be published on the strength of `headline`?"""
    if not buyer or not names_whole_word(buyer, headline):
        return False
    if not _type_stated(deal_type, headline):
        return False
    if not seller:
        return (deal_type or "").strip().lower() != "m&a"
    return any(_same_party(buyer, b) and _same_party(seller, s)
               for b, s in directional_pairs(headline))
