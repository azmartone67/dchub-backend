#!/usr/bin/env python3
"""
DC Hub — Utility Provider Extractor (Brain Intelligence Phase 2)
================================================================

Scans the announcements table for utility-buyer signals and updates
discovered_facilities.utility_provider when a high-confidence match is found.

Catches what geographic backfill misses:
  - Forward-looking PPAs ("AEP signed 100MW PPA with Amazon")
  - Transmission-level customers (large hyperscalers serviced direct from
    transmission, bypassing retail utility territory)
  - New campuses not yet near an indexed substation
  - Utility-stated relationships ("Dominion will deliver 500 MW to AWS")

Strategy:
  1. For each recent announcement, extract:
       - All utility name mentions (regex against curated alias list)
       - The operator(s) mentioned (AWS, Microsoft, Meta, Google, etc.)
       - Location signals (city, state, lat/lng if available)
  2. Build (operator, location, utility) triples with confidence scores.
  3. Match each triple to ONE specific facility in discovered_facilities.
  4. UPDATE that facility's utility_provider IF:
       - Confidence ≥ MIN_CONFIDENCE
       - Facility has no existing utility_provider OR confidence > existing
  5. Report stats.

Run standalone:
    python utility_extractor.py                  # 60 days lookback, dry run
    python utility_extractor.py --days 90 --apply  # 90 days, write changes
    python utility_extractor.py --apply --verbose  # see every match decision

Environment:
    NEON_DATABASE_URL or DATABASE_URL must be set.

Integration into autonomous_brain.py (later):
    Wrap as `extract_utility_from_news(self)` method following the existing
    extract_*_from_news pattern. Add to run_autonomous_cycle().
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import psycopg2
import psycopg2.extras

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("utility_extractor")

# ─────────────────────────────────────────────────────────────────────────────
#  UTILITY ALIAS MAP
# ─────────────────────────────────────────────────────────────────────────────
# Maps canonical utility name → list of (regex pattern, weight) tuples.
# Higher weight = more specific/unambiguous match.
# Patterns use word boundaries (\b) to avoid partial matches.
# ─────────────────────────────────────────────────────────────────────────────

UTILITY_ALIASES: dict[str, list[tuple[str, float]]] = {
    "AEP": [
        (r"\bAmerican Electric Power\b", 1.0),
        (r"\bAEP Ohio\b", 1.0),
        (r"\bAEP Texas\b", 1.0),
        (r"\bAppalachian Power\b", 0.9),
        (r"\bIndiana Michigan Power\b", 0.9),
        (r"\bKentucky Power\b", 0.9),
        (r"\bAEP\b", 0.8),  # bare "AEP" — could be ambiguous in some contexts
    ],
    "Dominion Energy": [
        (r"\bDominion Energy\b", 1.0),
        (r"\bDominion Virginia Power\b", 1.0),
        (r"\bVirginia Electric (?:and|&) Power\b", 1.0),
        (r"\bVEPCO\b", 0.9),
        (r"\bDominion\b(?!\s+(?:Resources|Midstream))", 0.7),
    ],
    "Duke Energy": [
        (r"\bDuke Energy\b", 1.0),
        (r"\bDuke Energy Carolinas\b", 1.0),
        (r"\bDuke Energy Progress\b", 1.0),
        (r"\bDuke Energy Florida\b", 1.0),
        (r"\bDuke Energy Indiana\b", 1.0),
        (r"\bDuke Energy Ohio\b", 1.0),
    ],
    "Georgia Power": [
        (r"\bGeorgia Power\b", 1.0),
    ],
    "Alabama Power": [
        (r"\bAlabama Power\b", 1.0),
    ],
    "Southern Company": [
        (r"\bSouthern Company\b", 1.0),
    ],
    "NextEra / FPL": [
        (r"\bFlorida Power (?:and|&) Light\b", 1.0),
        (r"\bNextEra Energy\b", 1.0),
        (r"\bFPL\b", 0.85),
    ],
    "PG&E": [
        (r"\bPacific Gas (?:and|&) Electric\b", 1.0),
        (r"\bPG&E\b", 1.0),
    ],
    "Southern California Edison": [
        (r"\bSouthern California Edison\b", 1.0),
        (r"\bSCE\b", 0.7),
    ],
    "PacifiCorp": [
        (r"\bPacifiCorp\b", 1.0),
        (r"\bPacific Power\b", 0.85),
        (r"\bRocky Mountain Power\b", 0.9),
    ],
    "Bonneville Power Administration": [
        (r"\bBonneville Power Administration\b", 1.0),
        (r"\bBPA\b(?!\s+(?:Worldwide|free))", 0.85),
    ],
    "Tennessee Valley Authority": [
        (r"\bTennessee Valley Authority\b", 1.0),
        (r"\bTVA\b", 0.9),
    ],
    "Xcel Energy": [
        (r"\bXcel Energy\b", 1.0),
        (r"\bNorthern States Power\b", 0.9),
        (r"\bPublic Service Co(?:mpany)? of Colorado\b", 0.9),
    ],
    "Entergy": [
        (r"\bEntergy\b", 1.0),
    ],
    "Exelon": [
        (r"\bExelon\b", 1.0),
        (r"\bComEd\b", 0.95),
        (r"\bCommonwealth Edison\b", 1.0),
        (r"\bBGE\b", 0.85),
        (r"\bBaltimore Gas (?:and|&) Electric\b", 1.0),
        (r"\bPECO\b", 0.85),
        (r"\bPepco\b", 0.9),
        (r"\bPotomac Electric\b", 0.9),
    ],
    "FirstEnergy": [
        (r"\bFirstEnergy\b", 1.0),
        (r"\bOhio Edison\b", 0.95),
        (r"\bToledo Edison\b", 0.95),
        (r"\bCleveland Electric Illuminating\b", 0.95),
        (r"\bJersey Central Power\b", 0.95),
        (r"\bMet-?Ed\b", 0.9),
        (r"\bWest Penn Power\b", 0.95),
    ],
    "Salt River Project": [
        (r"\bSalt River Project\b", 1.0),
        (r"\bSRP\b(?!\s+(?:bonds|debt))", 0.85),
    ],
    "Arizona Public Service": [
        (r"\bArizona Public Service\b", 1.0),
        (r"\bAPS\b(?!\s+(?:Inc|Corp))", 0.7),
    ],
    "MidAmerican": [
        (r"\bMidAmerican Energy\b", 1.0),
        (r"\bMidAmerican\b", 0.85),
    ],
    "National Grid": [
        (r"\bNational Grid\b", 1.0),
        (r"\bNiagara Mohawk\b", 0.95),
        (r"\bMassachusetts Electric\b", 0.95),
    ],
    "ConEd": [
        (r"\bCon Edison\b", 1.0),
        (r"\bConsolidated Edison\b", 1.0),
        (r"\bConEd\b", 1.0),
    ],
    "Eversource": [
        (r"\bEversource Energy\b", 1.0),
        (r"\bEversource\b", 1.0),
    ],
    "PSEG": [
        (r"\bPublic Service Enterprise Group\b", 1.0),
        (r"\bPSEG\b", 1.0),
        (r"\bPSE&G\b", 1.0),
    ],
    "AES Ohio": [
        (r"\bAES Ohio\b", 1.0),
        (r"\bDayton Power (?:and|&) Light\b", 1.0),
        (r"\bDP&L\b", 0.9),
    ],
    "Black Hills Energy": [
        (r"\bBlack Hills Energy\b", 1.0),
        (r"\bBlack Hills Power\b", 0.95),
    ],
    "Avangrid": [
        (r"\bAvangrid\b", 1.0),
        (r"\bIberdrola\b", 0.9),
    ],
    "OPPD": [
        (r"\bOmaha Public Power District\b", 1.0),
        (r"\bOPPD\b", 0.9),
    ],
    "NV Energy": [
        (r"\bNV Energy\b", 1.0),
    ],
    "Oncor": [
        (r"\bOncor\b", 1.0),
    ],
    "CenterPoint": [
        (r"\bCenterPoint Energy\b", 1.0),
        (r"\bCenterPoint\b", 0.9),
    ],
    "DTE Energy": [
        (r"\bDTE Energy\b", 1.0),
        (r"\bDetroit Edison\b", 0.95),
    ],
    "Consumers Energy": [
        (r"\bConsumers Energy\b", 1.0),
    ],
    "WEC Energy": [
        (r"\bWE Energies\b", 1.0),
        (r"\bWisconsin Public Service\b", 0.95),
        (r"\bWEC Energy\b", 1.0),
    ],
    "Alliant Energy": [
        (r"\bAlliant Energy\b", 1.0),
    ],
    "Ameren": [
        (r"\bAmeren\b", 1.0),
        (r"\bUnion Electric\b", 0.9),
    ],
    "CMS Energy": [
        (r"\bCMS Energy\b", 1.0),
    ],
    "Evergy": [
        (r"\bEvergy\b", 1.0),
        (r"\bWestar Energy\b", 0.9),
        (r"\bKansas City Power\b", 0.9),
    ],
    "Pinnacle West": [
        (r"\bPinnacle West\b", 1.0),
    ],
    "Southwestern Public Service": [
        (r"\bSouthwestern Public Service\b", 1.0),
        (r"\bSPS\b", 0.6),
    ],
    "El Paso Electric": [
        (r"\bEl Paso Electric\b", 1.0),
    ],
    "TECO": [
        (r"\bTampa Electric\b", 1.0),
        (r"\bTECO\b", 0.9),
    ],
    "Dominion Energy South Carolina": [
        (r"\bDominion Energy South Carolina\b", 1.0),
        (r"\bSouth Carolina Electric (?:and|&) Gas\b", 1.0),
        (r"\bSCE&G\b", 0.95),
    ],
    "Santee Cooper": [
        (r"\bSantee Cooper\b", 1.0),
    ],
    "Umatilla Electric Cooperative": [
        (r"\bUmatilla Electric Cooperative\b", 1.0),
        (r"\bUmatilla Electric\b", 0.95),
    ],
}

# ─────────────────────────────────────────────────────────────────────────────
#  HYPERSCALER ALIAS MAP — to identify which operator an announcement is about
# ─────────────────────────────────────────────────────────────────────────────

HYPERSCALER_ALIASES: dict[str, list[str]] = {
    "Amazon Web Services": [r"\bAmazon Web Services\b", r"\bAWS\b"],
    "Microsoft":           [r"\bMicrosoft\b", r"\bAzure\b", r"\bMSFT\b"],
    "Meta":                [r"\bMeta\b(?!\s+(?:platform|description|tag))", r"\bFacebook\b"],
    "Google":              [r"\bGoogle\b", r"\bAlphabet\b"],
    "Oracle":              [r"\bOracle\b"],
    "Apple":               [r"\bApple\b(?!\s+(?:Music|TV|App|iPhone))"],
    "CoreWeave":           [r"\bCoreWeave\b"],
    "Equinix":             [r"\bEquinix\b"],
    "Digital Realty":      [r"\bDigital Realty\b"],
    "Vantage Data Centers":[r"\bVantage Data Centers\b", r"\bVantage\b"],
    "QTS":                 [r"\bQTS\b"],
    "Iron Mountain":       [r"\bIron Mountain\b"],
    "Stack Infrastructure":[r"\bSTACK Infrastructure\b", r"\bSTACK\b"],
    "Lambda":              [r"\bLambda\b(?!\s+(?:function|expression))"],
}

# ─────────────────────────────────────────────────────────────────────────────
#  STATE / LOCATION ALIASES
# ─────────────────────────────────────────────────────────────────────────────

STATE_NAME_TO_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
}

MIN_CONFIDENCE = 0.65          # PATCH 2026-04-30: lowered from 0.7 — too strict on first pass
MAX_FACILITIES_PER_HIT = 8     # PATCH 2026-04-30: was strictly 1; allow multi-match
                                # so "Meta in LA served by Entergy" can update all Meta LA sites

# ─────────────────────────────────────────────────────────────────────────────
#  EXTRACTION CORE
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class UtilityMention:
    canonical_name: str
    matched_text: str
    confidence: float
    char_offset: int  # for proximity scoring


@dataclass
class HyperscalerMention:
    canonical_name: str
    matched_text: str
    char_offset: int


@dataclass
class ExtractionCandidate:
    """A potential (operator, utility, location) triple from one announcement."""
    article_id: str
    operator: str
    utility: str
    state: Optional[str]
    city: Optional[str]
    confidence: float
    proximity_score: float
    source_url: Optional[str]
    title: str


def find_utility_mentions(text: str) -> list[UtilityMention]:
    """Find all utility mentions in text with their positions and confidence."""
    found: list[UtilityMention] = []
    for canonical, patterns in UTILITY_ALIASES.items():
        for pattern, weight in patterns:
            for m in re.finditer(pattern, text, re.IGNORECASE):
                found.append(UtilityMention(
                    canonical_name=canonical,
                    matched_text=m.group(0),
                    confidence=weight,
                    char_offset=m.start(),
                ))
    return found


def find_hyperscaler_mentions(text: str) -> list[HyperscalerMention]:
    """Find all hyperscaler/operator mentions in text."""
    found: list[HyperscalerMention] = []
    for canonical, patterns in HYPERSCALER_ALIASES.items():
        for pattern in patterns:
            for m in re.finditer(pattern, text, re.IGNORECASE):
                found.append(HyperscalerMention(
                    canonical_name=canonical,
                    matched_text=m.group(0),
                    char_offset=m.start(),
                ))
    return found


def find_state(text: str) -> Optional[str]:
    """Find a US state mentioned in text, return 2-letter abbreviation."""
    # Try full state names first (more specific)
    for name, abbr in STATE_NAME_TO_ABBR.items():
        if re.search(rf"\b{re.escape(name)}\b", text, re.IGNORECASE):
            return abbr
    # Then 2-letter abbreviations (require comma or space-comma context to avoid false positives)
    m = re.search(r"\b([A-Z]{2})\b(?=[,\s])", text)
    if m and m.group(1) in STATE_NAME_TO_ABBR.values():
        return m.group(1)
    return None


def find_city(text: str, state: Optional[str]) -> Optional[str]:
    """Best-effort city extraction. Looks for "in <City>, <State>" patterns."""
    if state:
        m = re.search(rf"in\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){{0,2}}),\s*{state}\b", text)
        if m:
            return m.group(1).strip()
    # Fallback: "<City>, <ST>" anywhere
    m = re.search(r"([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){0,2}),\s+[A-Z]{2}\b", text)
    if m:
        return m.group(1).strip()
    return None


def extract_candidates(article: dict) -> list[ExtractionCandidate]:
    """Given an announcement row, build (operator, utility, location) candidates."""
    article_id = str(article.get("id", ""))
    title = article.get("title", "") or ""
    summary = article.get("summary", "") or ""
    content = article.get("content", "") or ""
    text = f"{title}\n{summary}\n{content}"

    if not text.strip():
        return []

    utilities = find_utility_mentions(text)
    operators = find_hyperscaler_mentions(text)

    if not utilities or not operators:
        return []

    state = find_state(text)
    city = find_city(text, state)

    candidates: list[ExtractionCandidate] = []

    # For each (operator, utility) pair in the article, compute proximity score
    # (the closer they appear in the text, the higher the confidence).
    for op in operators:
        for ut in utilities:
            distance = abs(op.char_offset - ut.char_offset)
            # Proximity scoring: same sentence (~200 chars) = 1.0, falls off with distance
            if distance <= 200:
                proximity = 1.0
            elif distance <= 500:
                proximity = 0.85
            elif distance <= 1500:
                proximity = 0.65
            else:
                proximity = 0.4

            confidence = ut.confidence * proximity
            if confidence < MIN_CONFIDENCE:
                continue

            candidates.append(ExtractionCandidate(
                article_id=article_id,
                operator=op.canonical_name,
                utility=ut.canonical_name,
                state=state,
                city=city,
                confidence=confidence,
                proximity_score=proximity,
                source_url=article.get("source_url"),
                title=title,
            ))

    return candidates


# ─────────────────────────────────────────────────────────────────────────────
#  FACILITY MATCHING
# ─────────────────────────────────────────────────────────────────────────────

def match_facility(cur, candidate: ExtractionCandidate) -> list[dict]:
    """Find matching facilities in discovered_facilities. Returns up to
    MAX_FACILITIES_PER_HIT rows.

    PATCH 2026-04-30 v2 (CORRUPTION FIX): REQUIRE candidate to have state OR
    city. Without ANY location context, an article mentioning 'Meta + Alliant
    Energy' would match every Meta facility globally — which corrupted 35
    facilities in the prior run. Now: no location context = refuse to update,
    return empty list."""
    if not candidate.state and not candidate.city:
        # No location signal extractable from the article. Refuse to update —
        # we'd otherwise be applying the utility to every facility owned by
        # this operator worldwide. Caller will count this as "no_match_found".
        return []

    operator_pattern = f"%{candidate.operator.split()[0]}%"  # e.g. "Amazon" → "%Amazon%"

    where_clauses = ["(provider ILIKE %s OR name ILIKE %s)"]
    params: list = [operator_pattern, operator_pattern]

    if candidate.state:
        where_clauses.append("state = %s")
        params.append(candidate.state)
    if candidate.city:
        where_clauses.append("city ILIKE %s")
        params.append(candidate.city)

    where = " AND ".join(where_clauses)
    cur.execute(
        f"""
        SELECT id, name, provider, city, state, utility_provider
        FROM discovered_facilities
        WHERE {where}
        LIMIT %s
        """,
        params + [MAX_FACILITIES_PER_HIT],
    )
    rows = cur.fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────

def get_db_url() -> str:
    url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL", "")
    if not url:
        logger.error("NEON_DATABASE_URL or DATABASE_URL must be set")
        sys.exit(2)
    return url


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=60, help="Lookback days for announcements")
    parser.add_argument("--apply", action="store_true", help="Actually write changes to DB (default: dry run)")
    parser.add_argument("--verbose", action="store_true", help="Show every match decision")
    parser.add_argument("--limit", type=int, default=10000, help="Max announcements to process")
    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    conn = psycopg2.connect(get_db_url(), connect_timeout=15)
    conn.autocommit = False

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            logger.info(
                "Loading announcements from last %d days (limit=%d)…",
                args.days, args.limit,
            )
            cur.execute(
                """
                SELECT id, title, COALESCE(content, summary, '') AS content,
                       summary, source_url, discovered_at
                FROM announcements
                WHERE discovered_at::timestamp > NOW() - INTERVAL '%s days'
                ORDER BY discovered_at DESC
                LIMIT %s
                """,
                (args.days, args.limit),
            )
            articles = cur.fetchall()
            logger.info("Loaded %d announcements.", len(articles))

            # Aggregate candidates across all articles, keyed by (operator, state, city)
            stats = {
                "articles_scanned": len(articles),
                "articles_with_signal": 0,
                "candidates_generated": 0,
                "candidates_above_threshold": 0,
                "facilities_updated": 0,
                "multi_match_hits": 0,    # candidates that hit >1 facility (now updated, was skipped)
                "ambiguous_matches": 0,   # legacy key — kept at 0 in new logic
                "no_match_found": 0,
                "already_correct": 0,
                "by_utility": defaultdict(int),
            }

            for article in articles:
                article_d = dict(article)
                cands = extract_candidates(article_d)
                if cands:
                    stats["articles_with_signal"] += 1
                    stats["candidates_generated"] += len(cands)

                # Best (highest confidence) candidate per (operator, utility) pair
                # avoids triple-counting same article
                best_per_pair: dict[tuple, ExtractionCandidate] = {}
                for c in cands:
                    key = (c.operator, c.utility)
                    if key not in best_per_pair or c.confidence > best_per_pair[key].confidence:
                        best_per_pair[key] = c

                for c in best_per_pair.values():
                    if c.confidence < MIN_CONFIDENCE:
                        continue
                    stats["candidates_above_threshold"] += 1
                    facilities = match_facility(cur, c)

                    if not facilities:
                        stats["no_match_found"] += 1
                        if args.verbose:
                            logger.debug(
                                "  no facility match: op=%s utility=%s state=%s city=%s",
                                c.operator, c.utility, c.state, c.city,
                            )
                        continue

                    # Track multi-match scenarios for visibility
                    if len(facilities) > 1:
                        stats["multi_match_hits"] += 1
                        if args.verbose:
                            logger.info(
                                "  MULTI-MATCH (%d facilities): op=%s state=%s city=%s utility=%s",
                                len(facilities), c.operator, c.state, c.city, c.utility,
                            )

                    # Update all matching facilities (was: only update if exactly 1).
                    # Rationale: when an announcement says "Meta is building data
                    # centers in Louisiana powered by Entergy", that single statement
                    # validly applies to every Meta LA facility, not just one.
                    for facility in facilities:
                        if facility.get("utility_provider") == c.utility:
                            stats["already_correct"] += 1
                            continue

                        if args.verbose:
                            logger.info(
                                "    → id=%s '%s' utility=%s (was: %s) conf=%.2f",
                                facility["id"], facility["name"], c.utility,
                                facility.get("utility_provider"), c.confidence,
                            )

                        if args.apply:
                            cur.execute(
                                """
                                UPDATE discovered_facilities
                                SET utility_provider = %s, last_updated = NOW()
                                WHERE id = %s
                                """,
                                (c.utility, facility["id"]),
                            )
                        stats["facilities_updated"] += 1
                        stats["by_utility"][c.utility] += 1

            if args.apply:
                conn.commit()
                logger.info("Changes COMMITTED to DB.")
            else:
                conn.rollback()
                logger.info("Dry run — no changes written. Re-run with --apply to commit.")

        # ─ Report ──────────────────────────────────────────────────────────
        logger.info("")
        logger.info("══════════════════════════════════════════════════════")
        logger.info("  EXTRACTION STATS")
        logger.info("══════════════════════════════════════════════════════")
        for k, v in stats.items():
            if k == "by_utility":
                continue
            logger.info("  %-30s %s", k, v)

        if stats["by_utility"]:
            logger.info("")
            logger.info("  Top utilities updated (or would-be-updated in dry run):")
            for util, n in sorted(stats["by_utility"].items(), key=lambda x: -x[1])[:20]:
                logger.info("    %-40s %d", util, n)

        return 0

    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
