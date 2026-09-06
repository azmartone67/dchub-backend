#!/usr/bin/env python3
"""
frontend_stat_normalizer.py — Fix All Number Inconsistencies Across DC Hub Pages
═════════════════════════════════════════════════════════════════════════════════

Run this locally against your Cloudflare Pages source directory.
Normalizes all conflicting stats to canonical values.

Usage:
    python3 frontend_stat_normalizer.py /path/to/cloudflare-pages-root

    # Dry run (show changes without writing):
    python3 frontend_stat_normalizer.py /path/to/cloudflare-pages-root --dry-run

What it fixes:
    1. Deals tracked: legacy $51B / $70B / $324B $-stats + retired row-count
       floors → the canonical DISTINCT deal floor
    2. MCP tools count: any non-canonical "<n> MCP tools" → the canonical count
    3. Facilities: the retired pre-dedup floors → the canonical facility floor
    4. Pipeline GW: 21+ GW → the canonical pipeline GW
    5. Title tag format: "DC Hub -" → "DC Hub —" (em dash, consistent)
    6. Tool count in meta/descriptions
    7. Developer tier feature list alignment

★2026-07-31 — this module used to hand-type its own numbers, and they had gone
stale in the most dangerous possible place: it is a REWRITER, so a stale value
here does not merely sit in a file, it gets WRITTEN INTO frontend copy. Worse,
CANONICAL called itself "single source of truth" but nothing read it — the
REPLACEMENTS table below carried a SECOND, independently-drifted copy of every
value, and the two openly disagreed (CANONICAL said 79 tools while REPLACEMENTS
rewrote pages to "20 MCP Tools"; CANONICAL said 15,000+ facilities while
REPLACEMENTS rewrote 21,000+ down to the retired 20,000+ floor; both wrote
"4,000+ deals", a value ai_surface_canon explicitly BANS as a duplicate-row
over-claim). Both tables now derive from ai_surface_canon.PINNED and
canonical_stats, and REPLACEMENTS is built FROM CANONICAL so the two can never
fork again. tests/test_canonical_counts_drift.py pins the wiring.

Author: DC Hub QA — March 28, 2026
"""

import os
import re
import sys
import glob

# Canon SoT. Import-safe by construction: ai_surface_canon touches only stdlib
# at module scope, and canonical_stats._FALLBACK is a static dict (no DB call —
# get_canonical_stats() would query, so it is deliberately NOT used here).
from ai_surface_canon import PINNED as _CANON
from canonical_stats import _FALLBACK as _CANON_FLOOR

_PUBLIC = _CANON.get('public') or {}


def _digits(phrase: str) -> str:
    """'15,000+' -> '15000'. The bare-number form some markup uses."""
    return re.sub(r'[^\d]', '', phrase or '')


# ═══════════════════════════════════════════════════════════════
# CANONICAL VALUES — rendered from the canon, never hand-typed
# ═══════════════════════════════════════════════════════════════
CANONICAL = {
    'facilities': _PUBLIC.get('facilities', ''),
    'facilities_number': _digits(_PUBLIC.get('facilities', '')),
    'countries': _PUBLIC.get('countries', ''),
    'deals_tracked': f"{_PUBLIC.get('deals', '')} deals",
    'pipeline_gw': f"{_CANON_FLOOR.get('pipeline_gw', '')} GW",
    'mcp_tools': str(_CANON.get('tools_advertised')
                     or len(_CANON.get('tool_manifest') or ())),
    'markets': _PUBLIC.get('markets', ''),
    'substations': f"{int(_CANON_FLOOR.get('substations', 0)):,}",
    # ★2026-09-06 r-news-sources: news_sources GRADUATED out of the block
    # below. It now has a canon home (PINNED['public']['news_sources'], derived
    # live by canonical_stats.news_sources_phrase()) and is read from it like
    # every other entry above. The hand-typed '40+' it replaces was not merely
    # underived — it was low by ~61x against a measured 2,442.
    'news_sources': _PUBLIC.get('news_sources', ''),
    # No canon home for these two — they are NOT fenced and NOT derived.
    # Give them one before quoting them anywhere agent-facing.
    # ★news_articles measured 15,050 on 2026-09-06 against the same corpus,
    #   i.e. this literal is stale-low too. It needs its own floor spec, not a
    #   hand-bump — see the news_sources wiring for the shape.
    'pipeline_projects': '540+',
    'news_articles': '13,900+',
}

# ═══════════════════════════════════════════════════════════════
# REPLACEMENT RULES
# ═══════════════════════════════════════════════════════════════
# Each rule: (pattern_to_find, replacement, description)
# Patterns are regex — use raw strings.
#
# ★Every REPLACEMENT value is read out of CANONICAL above. Writing a literal
# here is the bug this module shipped for four months: the two tables forked and
# the one that actually ran was the stale one.
_T = CANONICAL['mcp_tools']
_FAC = CANONICAL['facilities']
_DEALS = CANONICAL['deals_tracked']
_GW = CANONICAL['pipeline_gw']

# Any tool count that is not the canonical one, when qualified by "MCP" (so it
# is unambiguously OUR count, not prose). Generic rather than an enumerated list
# of retired values — pinning the last known bad number is how "11 -> 20" was
# still the rule while live went to 82. \s* also covers the "15MCP Tools" and
# "11MCP Tools" no-space variants the old table needed two extra rules for.
_STALE_MCP_TOOLS = rf'(?<![\d,])(?!{re.escape(_T)}\b)\d{{1,3}}\s*MCP [Tt]ools\b'

# Retired FACILITY floors. Same range shape as the drift-fence's
# facilities_stale_floor (19–23),NNN+ plus the separately-retired 12,650+ —
# 20,000+/21,000+/22,000+ were all live simultaneously on different pages, and
# the old rule normalized 21,000+ DOWN to 20,000+, i.e. one retired floor to
# another. 15,xxx (canonical) is outside the range and is left alone. The
# trailing lookahead spares square-footage ("20,000+ sq ft"), which is a real
# unit on these pages and not a facility count.
_STALE_FACILITY_FLOOR = (r'(?<![\d,])(?:(?:19|20|21|22|23),\d{3}\+|12,650\+)'
                         r'(?!\s*(?:sq\b|square))')

REPLACEMENTS = [
    # --- Deals tracked ---
    # ★Order matters: the "$70B+ volume" rule must precede the bare "$70B+" one
    # or the generic rule consumes it first and the nav variant never fires
    # (it never did — that rule was dead as written).
    (r'\$70B\+ volume', f"{_PUBLIC.get('deals', '')} tracked deals", 'deals nav: legacy $ stat → canonical distinct count'),
    (r'\$51B\+', _DEALS, 'deals: legacy $ stat → canonical distinct count'),
    (r'\$70B\+', _DEALS, 'deals: legacy $ stat → canonical distinct count'),
    # Retired ROW-count deal floors — these counted duplicate rows (the AUTO id
    # embeds the ingest date, so one deal accrues a row per day) and are on
    # ai_surface_canon's stale_markers denylist.
    (r'(?<![\d,])(?:2,000|2,200|3,000|4,000)\+\s+(?:tracked\s+)?(?:M&A\s+)?deals\b',
     _DEALS, 'deals: retired row-count floor → canonical distinct count'),

    # --- News sources ---
    # ★2026-09-06 r-news-sources. The retired "40+ sources" claim, in the two
    # shapes the frontend actually carries ("40+ sources" and "40+ news
    # sources"). Anchored on the trailing noun for the same reason the market
    # markers are: a bare 40 collides with any 40 MW / 40% on the page.
    (r'(?<![\d,])40\+\s+(news\s+)?sources\b',
     rf"{CANONICAL['news_sources']} \1sources",
     f"news sources: retired hand-typed floor -> {CANONICAL['news_sources']}"),

    # --- MCP tool count ---
    (_STALE_MCP_TOOLS, f'{_T} MCP Tools', f'tools: non-canonical → {_T}'),
    (r'(?<![\d,])(?:11|15|20)\s+([Tt])ools\b', rf'{_T} \1ools',
     f'tools: legacy bare count → {_T}'),

    # --- Facilities count ---
    (_STALE_FACILITY_FLOOR + r'\s+facilities', f'{_FAC} facilities', 'facilities: retired floor → canonical'),
    (_STALE_FACILITY_FLOOR, _FAC, 'facilities: retired floor → canonical (generic)'),
    # Don't touch "11,361 global facilities" in nav — that's the precise map count

    # --- Pipeline GW ---
    (r'21\+\s?GW', _GW, f'pipeline: 21+ GW → {_GW}'),

    # --- Title tag consistency (em dash) ---
    (r'DC Hub - Data Center', 'DC Hub — Data Center', 'title: hyphen → em dash'),

    # --- Developer tier: site analysis alignment ---
    # On /developers, Developer tier says "✗ Site analysis & scoring"
    # But /connect says all tools fully unlocked, including site analysis
    # Resolution: Developer DOES include site analysis (it's an MCP tool)
    (r'✗ Site analysis & scoring', '✓ Site analysis & scoring (MCP)', 'dev tier: unlock site analysis'),
    (r'✗ PDF reports & exports', '✗ PDF reports & exports', 'keep: PDF reports Pro-only'),
]

# Additional whole-line replacements for specific pages
PAGE_SPECIFIC_FIXES = {
    'developers.html': [
        # Normalize legacy deal $ stat → verified deal COUNT
        (r'\$51B\+Deals Tracked', f"{_PUBLIC.get('deals', '')}Deals Tracked", 'developers hero stat'),
        (r'\$51B\+', _DEALS, 'developers: all $51B references'),
    ],
    'connect.html': [
        # Fix tool count in header (incl. the no-space "15MCP Tools" variant)
        (_STALE_MCP_TOOLS, f'{_T} MCP Tools', 'connect header tool count'),
        # Fix pipeline GW
        (r'21\+\s?GWPipeline', f'{_GW}Pipeline', 'connect header pipeline'),
    ],
    'pricing.html': [
        # Fix facilities in free tier
        (_STALE_FACILITY_FLOOR + r'\s+facilities', f'{_FAC} facilities', 'pricing free tier'),
    ],
}


def process_file(filepath, dry_run=False):
    """Apply all replacements to a single HTML file."""
    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
    except Exception as e:
        print(f"  ⚠️  Could not read {filepath}: {e}")
        return 0

    original = content
    changes = []
    filename = os.path.basename(filepath)

    # Apply global replacements
    for pattern, replacement, desc in REPLACEMENTS:
        matches = re.findall(pattern, content)
        if matches:
            content = re.sub(pattern, replacement, content)
            changes.append(f"  {desc} ({len(matches)} occurrences)")

    # Apply page-specific fixes
    for page_pattern, fixes in PAGE_SPECIFIC_FIXES.items():
        if page_pattern in filename:
            for pattern, replacement, desc in fixes:
                matches = re.findall(pattern, content)
                if matches:
                    content = re.sub(pattern, replacement, content)
                    changes.append(f"  [page-specific] {desc} ({len(matches)} occurrences)")

    if content != original:
        if not dry_run:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(content)
        return changes
    return []


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 frontend_stat_normalizer.py /path/to/site/root [--dry-run]")
        print("\nThis script normalizes all conflicting stats across DC Hub HTML pages.")
        sys.exit(1)

    root_dir = sys.argv[1]
    dry_run = '--dry-run' in sys.argv

    if not os.path.isdir(root_dir):
        print(f"Error: {root_dir} is not a directory")
        sys.exit(1)

    if dry_run:
        print("🔍 DRY RUN — showing changes without writing\n")
    else:
        print("🔧 Applying stat normalization fixes\n")

    # Find all HTML files
    html_files = glob.glob(os.path.join(root_dir, '**', '*.html'), recursive=True)
    html_files += glob.glob(os.path.join(root_dir, '*.html'))
    html_files = list(set(html_files))  # dedupe

    if not html_files:
        print(f"No HTML files found in {root_dir}")
        sys.exit(1)

    print(f"Found {len(html_files)} HTML files\n")

    total_changes = 0
    files_changed = 0

    for filepath in sorted(html_files):
        rel_path = os.path.relpath(filepath, root_dir)
        changes = process_file(filepath, dry_run)
        if changes:
            files_changed += 1
            total_changes += len(changes)
            status = "WOULD CHANGE" if dry_run else "CHANGED"
            print(f"📝 {status}: {rel_path}")
            for change in changes:
                print(change)
            print()

    print(f"\n{'=' * 60}")
    print(f"Summary: {total_changes} changes across {files_changed} files")
    if dry_run:
        print("Run without --dry-run to apply changes.")
    else:
        print("✅ All changes applied. Redeploy to Cloudflare Pages.")

    # Print canonical values for reference
    print(f"\n📋 Canonical values used:")
    for key, val in CANONICAL.items():
        print(f"   {key}: {val}")


if __name__ == '__main__':
    main()
