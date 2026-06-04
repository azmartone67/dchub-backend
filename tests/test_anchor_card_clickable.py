"""r48.0 (2026-05-27) — anchor-card click-region lint.

The bug pattern: `<a class="card" ...>` or `<a class="card-link">…
<div class="card">…</div></a>` in a CSS grid layout WITHOUT explicit
`display: block` on the anchor. Default-inline anchors collapse to a
~0px tall hit region in CSS Grid, so the card looks clickable
(cursor:pointer + hover lift) but clicks never navigate.

This pattern hit /dcpi (r47.44), /dcgi (r47.45), /operators/<slug> and
/vs (both r47.46) on the same day — once we spotted it the user
immediately found two more pages. This lint catches the class so future
PRs can't reintroduce it.

For every routes/*.py file that contains `class="card"` or
`class="card-link"`, this test:

  1. Confirms there's at least one `<a ...>` element using those classes
  2. Verifies the SAME file's CSS has `.card-link{...display:block...}`
     OR `.card{...display:block...}` (whichever is the styled class)
  3. Files that use the class as a child inside a non-clickable parent
     (informational KPI cards) are exempt via the OPT_OUT list below.

Run locally:
    python -m pytest tests/test_anchor_card_clickable.py -v
"""
from __future__ import annotations

import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ROUTES_DIR = REPO_ROOT / "routes"

# Files where .card/.card-link are non-clickable (KPI tiles etc.). We
# verified manually these don't wrap anchors. If you add a clickable
# card pattern in any of these later, REMOVE the file from this list.
OPT_OUT = {
    "intelligence_dashboard.py",  # static KPI tiles, no anchors
    "operators.py",                # KPI tiles AND clickable cards — has display:block, verified r47.46
    "alive.py",                    # status grid, no anchors
    "changelog_landing.py",        # timeline cards, no anchors
}


def _scan_file(path: Path):
    """Returns dict of {anchor_class_used, has_display_block, errors}."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    # Detect EXACT class names (no prefix/suffix). A class="grid-card" or
    # class="stat-card" is a DIFFERENT class with its own CSS rule, NOT a
    # generic .card. r48.0 fix to the false-positive that flagged
    # grid_public_routes.py (which uses .grid-card) on this lint's first run.
    # Patterns: `class="card"`, `class="card foo"`, `class="foo card"`,
    # `class="foo card bar"`.
    used_card_link = bool(re.search(
        r"""<a\b[^>]*\bclass=("|')(?:[^"']*\s)?card-link(?:\s[^"']*)?\1""", text
    ))
    used_card = bool(re.search(
        r"""<a\b[^>]*\bclass=("|')(?:[^"']*\s)?card(?:\s[^"']*)?\1""", text
    ))
    if not (used_card or used_card_link):
        return None  # no clickable card pattern → skip

    # Find ALL .card-link and .card CSS rule bodies in the file and
    # check whether display:block (or inline-block) is set.
    def _rule_has_block(class_name: str) -> bool:
        # Look for the selector followed by { ... } body. Across the
        # codebase we use either `.foo{...}` or `.foo {...}` and the
        # rule body sometimes contains nested braces (CSS variables),
        # so the simplest approach is to scan for the selector then
        # walk to the next `}` accounting for one level of nesting.
        for m in re.finditer(r'\.' + re.escape(class_name) + r'\b[^{,]*\{', text):
            start = m.end()
            depth = 1
            i = start
            while i < len(text) and depth > 0:
                if text[i] == '{':
                    depth += 1
                elif text[i] == '}':
                    depth -= 1
                i += 1
            body = text[start:i - 1]
            if re.search(r'\bdisplay\s*:\s*(block|inline-block|grid|flex)\b', body):
                return True
        return False

    findings = []
    if used_card_link and not _rule_has_block("card-link"):
        findings.append(
            f"`<a class=\"card-link\">` used but `.card-link` CSS in same "
            f"file lacks `display: block`. Inline anchors collapse to ~0px "
            f"hit region in CSS Grid — cards look clickable but clicks land "
            f"on the inner div (no href). Fix: add `display:block;"
            f"text-decoration:none;color:inherit` to `.card-link`."
        )
    if used_card and not _rule_has_block("card"):
        findings.append(
            f"`<a class=\"card\">` used but `.card` CSS in same file lacks "
            f"`display: block`. Same click-region bug. Fix: add "
            f"`display:block;text-decoration:none;color:inherit` to `.card`."
        )

    return findings


def test_no_anchor_card_without_display_block():
    """Fence: every file with `<a class=\"card\">` or `card-link` markup
    must have `display:block` in its matching CSS rule."""
    if not ROUTES_DIR.exists():
        import pytest
        pytest.skip(f"routes/ not found at {ROUTES_DIR}")

    all_findings: list[tuple[str, list[str]]] = []
    for path in sorted(ROUTES_DIR.glob("*.py")):
        if path.name in OPT_OUT:
            continue
        findings = _scan_file(path)
        if findings:
            all_findings.append((path.name, findings))

    if all_findings:
        report = ["Anchor-as-card click-region bug found:", ""]
        for fname, msgs in all_findings:
            report.append(f"  routes/{fname}")
            for m in msgs:
                # Wrap message lines for readable pytest output.
                report.append(f"    - {m}")
            report.append("")
        report.append(
            "Reference fixes: r47.44 (dcpi.py), r47.45 (dcgi.py), "
            "r47.46 (operators.py / competitive_seo.py)."
        )
        raise AssertionError("\n".join(report))


if __name__ == "__main__":
    try:
        test_no_anchor_card_without_display_block()
        print("OK: no anchor-card click-region bugs found.")
    except AssertionError as e:
        print(str(e))
        raise SystemExit(1)
