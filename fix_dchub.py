#!/usr/bin/env python3
"""
DC Hub Glacier Site Fixer
=========================
Fix 1: _redirects — remove the /ai -> /ai-hub redirect so /ai loads ai.html directly
Fix 2: Restore dchub-nav.js across all HTML pages — remove old inline navs, inject script tag

Run from the root of your unzipped site folder:
    python fix_dchub.py

Or specify a path:
    python fix_dchub.py /path/to/site
"""

import os
import re
import sys

# ─── CONFIG ────────────────────────────────────────────────────────
SITE_DIR = sys.argv[1] if len(sys.argv) > 1 else "."
NAV_SCRIPT_TAG = '<script src="/js/dchub-nav.js"></script>'

# Pages that should NOT get the universal nav (app pages with their own layout)
SKIP_NAV_INJECTION = {
    "offline.html",
    "structured-data-head-snippet.html",
}

# ─── STATS ─────────────────────────────────────────────────────────
stats = {
    "redirects_fixed": 0,
    "navs_removed": 0,
    "headers_removed": 0,
    "market_navs_removed": 0,
    "script_tags_added": 0,
    "files_processed": 0,
    "files_skipped": 0,
    "errors": [],
}


def fix_redirects():
    """Fix 1: Remove the /ai -> /ai-hub 301 redirect."""
    rpath = os.path.join(SITE_DIR, "_redirects")
    if not os.path.exists(rpath):
        print("[WARN] _redirects file not found at", rpath)
        return

    with open(rpath, "r", encoding="utf-8") as f:
        lines = f.readlines()

    new_lines = []
    for line in lines:
        stripped = line.strip()
        # Match:  /ai   /ai-hub   301
        if re.match(r"^/ai\s+/ai-hub\s+30[12]", stripped):
            new_lines.append(f"# REMOVED — /ai should serve ai.html directly\n")
            new_lines.append(f"# {stripped}\n")
            stats["redirects_fixed"] += 1
            print(f"  [FIX] Commented out redirect: {stripped}")
        else:
            new_lines.append(line)

    with open(rpath, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

    print(f"[OK] _redirects fixed ({stats['redirects_fixed']} rule(s) removed)\n")


def remove_inline_navs(html, filepath):
    """Remove old inline nav blocks so dchub-nav.js can inject its own."""
    original = html
    relpath = os.path.relpath(filepath, SITE_DIR)

    # Classes that indicate a NON-site-nav (don't remove these)
    KEEP_NAV_CLASSES = {
        "dchub-nav",           # injected by dchub-nav.js
        "dchub-bottom-nav",    # injected by dchub-nav.js
        "admin-nav",
        "sidebar",
        "mobile-nav-drawer-links",
        "mobile-bottom-nav",
        "bottom-nav",
    }

    # ── Pattern 2: <header class="header">...</header> wrapping nav (facility/location pages)
    # Do this FIRST so we don't orphan content
    pattern2 = re.compile(
        r'<header\s+class="header"\s*>.*?</header>\s*',
        re.DOTALL
    )
    matches2 = pattern2.findall(html)
    if matches2:
        html = pattern2.sub('', html)
        stats["headers_removed"] += len(matches2)

    # ── Pattern 1: <nav ...>...</nav> — remove site-level navs
    # Matches any <nav> block, but skips ones with protected classes
    def nav_replacer(m):
        tag = m.group(0)
        opening = m.group(1)  # the opening <nav ...> tag
        # Check if it has a class we want to keep
        class_match = re.search(r'class="([^"]*)"', opening)
        if class_match:
            classes = class_match.group(1).split()
            for c in classes:
                if c in KEEP_NAV_CLASSES:
                    return tag  # keep it
        stats["navs_removed"] += 1
        return ''

    pattern1 = re.compile(
        r'(<nav(?:\s[^>]*)?>)(.*?)</nav>\s*',
        re.DOTALL
    )
    html = pattern1.sub(nav_replacer, html)

    return html


def inject_nav_script(html, filepath):
    """Add <script src="/js/dchub-nav.js"></script> before </body> if not present."""
    if "dchub-nav.js" in html:
        return html  # Already has it

    # Determine correct relative path based on file depth
    relpath = os.path.relpath(filepath, SITE_DIR)
    depth = relpath.count(os.sep)

    # For Cloudflare Pages, absolute paths (/js/...) work from any depth
    tag = NAV_SCRIPT_TAG

    # Insert before </body>
    if "</body>" in html:
        html = html.replace("</body>", f"{tag}\n</body>", 1)
        stats["script_tags_added"] += 1
    elif "</html>" in html:
        html = html.replace("</html>", f"{tag}\n</html>", 1)
        stats["script_tags_added"] += 1

    return html


def process_html_files():
    """Fix 2: Walk all HTML files, remove inline navs, add nav script."""
    print("[NAV] Scanning HTML files...\n")

    for root, dirs, files in os.walk(SITE_DIR):
        # Skip static/ directory (build artifacts, not pages)
        if "static" in dirs:
            dirs.remove("static")

        for fname in sorted(files):
            if not fname.endswith(".html"):
                continue

            filepath = os.path.join(root, fname)
            relpath = os.path.relpath(filepath, SITE_DIR)

            # Skip certain files
            if fname in SKIP_NAV_INJECTION:
                stats["files_skipped"] += 1
                continue

            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    html = f.read()

                original = html

                # Step 1: Remove old inline navs
                html = remove_inline_navs(html, filepath)

                # Step 2: Inject nav script tag
                html = inject_nav_script(html, filepath)

                # Only write if changed
                if html != original:
                    with open(filepath, "w", encoding="utf-8") as f:
                        f.write(html)
                    stats["files_processed"] += 1

            except Exception as e:
                stats["errors"].append(f"{relpath}: {e}")


def print_summary():
    print("\n" + "=" * 60)
    print("  DC HUB GLACIER FIX SUMMARY")
    print("=" * 60)
    print(f"  _redirects rules fixed:     {stats['redirects_fixed']}")
    print(f"  Inline <nav> removed:       {stats['navs_removed']}")
    print(f"  <header> wrappers removed:  {stats['headers_removed']}")
    print(f"  Market <nav> removed:       {stats['market_navs_removed']}")
    print(f"  Nav script tags injected:   {stats['script_tags_added']}")
    print(f"  HTML files modified:        {stats['files_processed']}")
    print(f"  HTML files skipped:         {stats['files_skipped']}")
    if stats["errors"]:
        print(f"\n  ERRORS ({len(stats['errors'])}):")
        for e in stats["errors"][:10]:
            print(f"    - {e}")
    print("=" * 60)
    print("\nDone! Your site is ready to deploy to Cloudflare Pages.")
    print("The /ai path will now serve ai.html directly.")
    print("All pages now load dchub-nav.js for the 6-dropdown Glacier nav.\n")


if __name__ == "__main__":
    print(f"\nDC Hub Glacier Site Fixer")
    print(f"Site directory: {os.path.abspath(SITE_DIR)}\n")

    if not os.path.isdir(SITE_DIR):
        print(f"[ERROR] Directory not found: {SITE_DIR}")
        sys.exit(1)

    # Check for _redirects to confirm we're in the right folder
    if not os.path.exists(os.path.join(SITE_DIR, "_redirects")):
        print("[WARN] No _redirects file found — are you in the site root?")
        resp = input("Continue anyway? (y/n): ").strip().lower()
        if resp != "y":
            sys.exit(0)

    print("[REDIRECTS] Fixing _redirects file...")
    fix_redirects()

    print("[NAV] Fixing navigation across all HTML pages...")
    process_html_files()

    print_summary()
