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
    1. Deals tracked: $51B / $70B / $324B → $324B+ (canonical)
    2. MCP tools count: 11 / 15 → 20 (actual count from /connect)
    3. Facilities: 21,000+ → 20,000+ (consistent with hero)
    4. Pipeline GW: 21+ GW → 369 GW (matches homepage + nav)
    5. Title tag format: "DC Hub -" → "DC Hub —" (em dash, consistent)
    6. Tool count in meta/descriptions
    7. Developer tier feature list alignment

Author: DC Hub QA — March 28, 2026
"""

import os
import re
import sys
import glob

# ═══════════════════════════════════════════════════════════════
# CANONICAL VALUES — Single source of truth
# ═══════════════════════════════════════════════════════════════
CANONICAL = {
    'facilities': '20,000+',
    'facilities_number': '20000',
    'countries': '140+',
    'deals_tracked': '$324B+',
    'pipeline_projects': '540+',
    'pipeline_gw': '369 GW',
    'mcp_tools': '20',
    'markets': '44',
    'substations': '79,755',
    'news_sources': '40+',
    'news_articles': '13,900+',
}

# ═══════════════════════════════════════════════════════════════
# REPLACEMENT RULES
# ═══════════════════════════════════════════════════════════════
# Each rule: (pattern_to_find, replacement, description)
# Patterns are regex — use raw strings

REPLACEMENTS = [
    # --- Deals tracked ---
    (r'\$51B\+', '$324B+', 'deals: $51B+ → $324B+'),
    (r'\$70B\+', '$324B+', 'deals: $70B+ → $324B+'),
    (r'\$70B\+ volume', '$324B+ volume', 'deals nav: $70B+ → $324B+'),

    # --- MCP tool count ---
    (r'\b11 MCP [Tt]ools\b', '20 MCP Tools', 'tools: 11 → 20'),
    (r'\b11 [Tt]ools\b', '20 Tools', 'tools: 11 → 20'),
    (r'\b15 MCP [Tt]ools\b', '20 MCP Tools', 'tools: 15 → 20'),
    (r'\b15 [Tt]ools\b', '20 Tools', 'tools: 15 → 20'),
    (r'15MCP Tools', '20 MCP Tools', 'tools: 15 → 20 (no space variant)'),
    (r'11MCP Tools', '20 MCP Tools', 'tools: 11 → 20 (no space variant)'),

    # --- Facilities count ---
    (r'21,000\+ facilities', '20,000+ facilities', 'facilities: 21K → 20K'),
    (r'21,000\+', '20,000+', 'facilities: 21K → 20K (generic)'),
    # Don't touch "11,361 global facilities" in nav — that's the precise map count

    # --- Pipeline GW ---
    (r'21\+ GW', '369 GW', 'pipeline: 21+ GW → 369 GW'),
    (r'21\+GW', '369 GW', 'pipeline: 21+GW → 369 GW (no space)'),

    # --- Title tag consistency (em dash) ---
    (r'DC Hub - Data Center', 'DC Hub — Data Center', 'title: hyphen → em dash'),

    # --- Developer tier: site analysis alignment ---
    # On /developers, Developer tier says "✗ Site analysis & scoring"
    # But /connect says "All 20 tools fully unlocked" including site analysis
    # Resolution: Developer DOES include site analysis (it's an MCP tool)
    (r'✗ Site analysis & scoring', '✓ Site analysis & scoring (MCP)', 'dev tier: unlock site analysis'),
    (r'✗ PDF reports & exports', '✗ PDF reports & exports', 'keep: PDF reports Pro-only'),
]

# Additional whole-line replacements for specific pages
PAGE_SPECIFIC_FIXES = {
    'developers.html': [
        # Fix the stat mismatch: $51B+ → $324B+
        (r'\$51B\+Deals Tracked', '$324B+Deals Tracked', 'developers hero stat'),
        (r'\$51B\+', '$324B+', 'developers: all $51B references'),
    ],
    'connect.html': [
        # Fix tool count in header
        (r'15MCP Tools', '20 MCP Tools', 'connect header tool count'),
        (r'15 MCP Tools', '20 MCP Tools', 'connect header tool count'),
        # Fix pipeline GW
        (r'21\+ GWPipeline', '369 GWPipeline', 'connect header pipeline'),
    ],
    'pricing.html': [
        # Fix facilities in free tier
        (r'21,000\+ facilities', '20,000+ facilities', 'pricing free tier'),
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
