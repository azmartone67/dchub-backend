"""r48.0 (2026-05-27) — canonical-field lint.

Why this exists: every other week a page on dchub.cloud reads a deprecated
field from /api/v1/stats and starts showing stale numbers (12,907
facilities instead of 21,433; "main_facilities" vs "total_facilities";
"d.stats" vs "d.data"). We fix one, then the same bug pattern reappears
on the next page added.

This test fences the class of bug. It scans `dchub-frontend/` for known
anti-pattern field reads in HTML/JS and FAILS the build if any are
found. New anti-patterns can be appended to ANTI_PATTERNS below.

Run locally:
    python -m pytest tests/test_canonical_fields.py -v

Run in CI: included in the regression-lint workflow alongside the other
fences in tests/test_honest_numbers.py.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# Root of the live frontend repo (the one Pages serves).
FRONTEND_ROOT = Path(os.environ.get(
    "DCHUB_FRONTEND_ROOT",
    str(Path(__file__).resolve().parent.parent.parent / "dchub-frontend"),
))


# ─── Field-name anti-patterns ────────────────────────────────────────────
# Each entry is (regex, msg, require_companion). The companion field, when
# present, is a string that MUST appear earlier on the same LINE as the
# regex match for the line to be considered OK. This lets us distinguish
# "uses `main_facilities` as primary" (bad) from "falls back to
# `main_facilities` after `total_facilities`" (fine).
ANTI_PATTERNS = [
    # /api/v1/stats: facility count
    (
        r"\bdata\.main_facilities\b",
        "Reads `data.main_facilities` (12,907 legacy `facilities` table) "
        "without preferring `data.total_facilities` (21,433 canonical) "
        "earlier on the same line. Use `data.total_facilities || "
        "data.discovered_facilities_raw || data.main_facilities` so the "
        "canonical value wins. See r47.47 / by-the-numbers fix.",
        "data.total_facilities",
    ),
    # /api/v1/stats: response envelope shape — `d.stats || d` is the bug;
    # `d.data || d.stats || d` is the fix.
    (
        r"\bd\.stats\s*\|\|\s*d\b",
        "Reads `d.stats || d` from /api/v1/stats response. The envelope is "
        "`{ data: {...} }`, not `{ stats: {...} }`. Use "
        "`d.data || d.stats || d` so the nested `data` object is read first. "
        "See r48.0 / about.html fix.",
        "d.data",
    ),
    # /api/v1/stats: countries field
    (
        r"\bdata\.countries\b",
        "Reads `data.countries` directly. Prefer "
        "`data.total_countries || data.countries` so the canonical field "
        "wins. See r48.0.",
        "data.total_countries",
    ),
    # Generic: facility count from the deprecated rollup field
    (
        r"\blegacy_facilities_table\b",
        "References the deprecated `legacy_facilities_table` field. The "
        "canonical count is `total_facilities` (alias: "
        "`discovered_facilities_raw`).",
        None,
    ),
]

# Files we explicitly DON'T lint — these contain the anti-patterns as
# REFERENCE strings (this file itself, comments explaining the bug, etc).
EXEMPT_FILES = {
    "tests/test_canonical_fields.py",
    "tests/test_honest_numbers.py",
    "HEALTH_BASELINE.md",
    "PATCHES",
    "WAVE_LOG.md",
}


def _is_exempt(path: Path) -> bool:
    return any(seg in str(path) for seg in EXEMPT_FILES)


def _scan_file(path: Path) -> list[tuple[int, str, str, str]]:
    """Returns list of (line_number, matched_text, file_relpath, message)."""
    findings: list[tuple[int, str, str, str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except (OSError, UnicodeDecodeError):
        return findings
    # Pre-split into lines so we can do companion-on-same-line checks.
    lines = text.splitlines()
    for entry in ANTI_PATTERNS:
        # Backward compat: support old 2-tuple entries.
        if len(entry) == 2:
            pattern, msg = entry
            companion = None
        else:
            pattern, msg, companion = entry
        for m in re.finditer(pattern, text):
            line_no = text[:m.start()].count("\n") + 1
            line_text = lines[line_no - 1] if 0 < line_no <= len(lines) else ""
            match_idx_in_line = line_text.find(m.group(0))
            # Skip if the match is inside a // comment or inside a string
            # literal that's being used as an explanation. Cheap check: if
            # `//`, `<!--`, or backtick-quoted explanation appears BEFORE
            # the match on the same line, it's a comment, not code.
            stripped = line_text.lstrip()
            if stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*") or stripped.startswith("<!--"):
                continue
            # Inline comment: `<code> // ...d.stats || d ...`
            slash_idx = line_text.find("//")
            if 0 <= slash_idx < match_idx_in_line:
                continue
            # Markdown-style backtick wrap: ``d.stats || d`` — explanation
            if line_text[:match_idx_in_line].count("`") % 2 == 1:
                continue
            # If a companion field is required and APPEARS on the same line
            # BEFORE the match, the line is fine (fallback pattern, not
            # primary read).
            if companion:
                comp_idx = line_text.find(companion)
                if 0 <= comp_idx < match_idx_in_line:
                    continue
            findings.append((line_no, m.group(0), str(path), msg))
    return findings


def _iter_frontend_files():
    """Yield HTML, JS, and template files under FRONTEND_ROOT (recursive)."""
    if not FRONTEND_ROOT.exists():
        return
    for ext in ("*.html", "*.js", "*.htm", "*.template"):
        for path in FRONTEND_ROOT.rglob(ext):
            if _is_exempt(path):
                continue
            if any(seg in path.parts for seg in ("node_modules", "dist", "build", ".git")):
                continue
            yield path


def test_no_canonical_field_anti_patterns():
    """Fence: no file in dchub-frontend may use a known wrong field read."""
    if not FRONTEND_ROOT.exists():
        # Frontend repo not checked out — skip rather than fail (CI doesn't
        # always have the sibling repo).
        import pytest
        pytest.skip(
            f"dchub-frontend not found at {FRONTEND_ROOT}. Set "
            "DCHUB_FRONTEND_ROOT to enable this fence."
        )

    findings: list[tuple[int, str, str, str]] = []
    for path in _iter_frontend_files():
        findings.extend(_scan_file(path))

    if findings:
        report_lines = ["Found canonical-field anti-patterns:", ""]
        for line_no, match, file_path, msg in findings:
            # Make path relative to frontend root for readable output.
            try:
                rel = Path(file_path).relative_to(FRONTEND_ROOT)
            except ValueError:
                rel = Path(file_path)
            report_lines.append(f"  {rel}:{line_no}")
            report_lines.append(f"    match: {match}")
            report_lines.append(f"    why:   {msg}")
            report_lines.append("")
        raise AssertionError("\n".join(report_lines))


if __name__ == "__main__":
    # Allow running as `python tests/test_canonical_fields.py` for quick checks.
    try:
        test_no_canonical_field_anti_patterns()
        print("OK: no canonical-field anti-patterns found.")
    except AssertionError as e:
        print(str(e))
        raise SystemExit(1)
