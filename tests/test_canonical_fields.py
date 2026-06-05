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


# ─── Backend-specific anti-patterns ──────────────────────────────────────
# Same shape as ANTI_PATTERNS but only applied to backend .py files. The
# backend has its own SQL-level canonical-table trap: COUNT(*) FROM
# facilities returns 12,907 (legacy), COUNT(*) FROM discovered_facilities
# returns 21,433 (canonical). Both /daily AND the brain inspector hit this
# in different forms.
BACKEND_ANTI_PATTERNS = [
    (
        r"SELECT\s+COUNT\(\*\)\s+FROM\s+facilities\b",
        "Runs `SELECT COUNT(*) FROM facilities` — that's the legacy table "
        "(~12,907 rows). The canonical table is `discovered_facilities` "
        "(~21,433 rows). The Inspector brain that powers /daily's narrative "
        "hit this trap and narrated '12,907 facilities tracked' on a page "
        "the homepage says is 21,000+. Use `discovered_facilities` for the "
        "headline count; keep `facilities` only if you specifically need "
        "the curated subset. See r48.1.",
        "discovered_facilities",
    ),
]

# Files we explicitly DON'T lint — these contain the anti-patterns as
# REFERENCE strings (this file itself, comments explaining the bug, etc).
EXEMPT_FILES = {
    "tests/test_canonical_fields.py",
    "tests/test_honest_numbers.py",
    "tests/test_anchor_card_clickable.py",
    "tests/test_pro_paywall_visibility.py",
    "HEALTH_BASELINE.md",
    "PATCHES",
    "WAVE_LOG.md",
    # r48.2: the bug-squash scanner contains the anti-patterns as detector
    # examples — exclude so it doesn't lint itself.
    "scripts/bug_squash.py",
    "routes/brain_bug_squash.py",
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


def _iter_backend_files():
    """Yield Python files under routes/ + main.py (the backend scope)."""
    backend_root = Path(__file__).resolve().parent.parent
    routes_dir = backend_root / "routes"
    if routes_dir.exists():
        for path in routes_dir.glob("*.py"):
            if _is_exempt(path):
                continue
            yield path
    main_py = backend_root / "main.py"
    if main_py.exists() and not _is_exempt(main_py):
        yield main_py


def _scan_file_with_patterns(path: Path, patterns) -> list[tuple[int, str, str, str]]:
    """Generalized scan that takes its own pattern list (for backend vs
    frontend lint runs).

    Per-line opt-out: append `# lint: legacy-facilities-ok` (or use a
    `legacy-facilities-ok` annotation in the preceding comment) to
    deliberately suppress a finding on a known-intentional query.
    """
    findings: list[tuple[int, str, str, str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except (OSError, UnicodeDecodeError):
        return findings
    lines = text.splitlines()
    OPT_OUT_TOKENS = ("legacy-facilities-ok", "lint-ok", "lint:ignore")
    for entry in patterns:
        if len(entry) == 2:
            pattern, msg = entry
            companion = None
        else:
            pattern, msg, companion = entry
        for m in re.finditer(pattern, text, re.IGNORECASE):
            line_no = text[:m.start()].count("\n") + 1
            line_text = lines[line_no - 1] if 0 < line_no <= len(lines) else ""
            match_idx_in_line = line_text.find(m.group(0))
            stripped = line_text.lstrip()
            # Skip Python comments + docstring lines + HTML comments.
            if (stripped.startswith("#") or stripped.startswith("//")
                    or stripped.startswith("/*") or stripped.startswith("*")
                    or stripped.startswith("<!--")):
                continue
            slash_idx = line_text.find("//")
            if 0 <= slash_idx < match_idx_in_line:
                continue
            hash_idx = line_text.find("#")
            if 0 <= hash_idx < match_idx_in_line:
                continue
            if line_text[:match_idx_in_line].count("`") % 2 == 1:
                continue
            if companion:
                comp_idx = line_text.find(companion)
                if 0 <= comp_idx < match_idx_in_line:
                    continue
            # Per-line opt-out: check the current line + the 2 lines above
            # for a deliberate suppression annotation.
            ctx_lines = lines[max(0, line_no - 3):line_no]
            ctx = " ".join(ctx_lines).lower()
            if any(tok in ctx for tok in OPT_OUT_TOKENS):
                continue
            findings.append((line_no, m.group(0), str(path), msg))
    return findings


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


# r48.1 (2026-05-27, refined r48.2): baseline of KNOWN backend canonical-
# table findings, by file + COUNT.
#
# When the test was first introduced it exposed 17+ `COUNT(*) FROM
# facilities` queries across main.py and helpers. Fixing them all in a
# single sprint risks breaking behaviors that nobody documented — so the
# test enforces "no NEW additions" instead of demanding "fix everything
# now". Today's baseline is locked below; new entries fail CI loudly.
#
# Earlier version of this baseline tracked exact line numbers; r48.2
# switched to file-level COUNTS because line numbers drift every time
# main.py is edited. Now we only require that the count per file
# doesn't INCREASE — additions fail, removals (cleanups) reduce the
# baseline naturally.
#
# To resolve a baseline entry: either (1) change the query to
# `discovered_facilities` and decrement the count, or (2) add a
# `# lint: legacy-facilities-ok` annotation explaining why the legacy
# table IS the intentional source for that query, and decrement the
# count.
KNOWN_BACKEND_FINDINGS: dict[str, int] = {
    "main.py": 11,
    "routes/brain_inspector.py": 1,  # secondary signal — see r48.1
    "routes/sitemap_auto.py":     1,
    "routes/site_stats.py":       2,
    "routes/facilities_by_dims.py": 2,
}


def test_no_backend_canonical_table_anti_patterns():
    """Fence: backend (routes/, main.py) may not COUNT(*) from the legacy
    `facilities` table for headline numbers. Use `discovered_facilities`.

    Known-pending findings from the r48.1 sweep are baselined above by
    FILE+COUNT (not line number, since line numbers drift). New
    occurrences in a file that exceed the baseline fail CI; new fixes
    can shrink the baseline."""
    backend_findings: list[tuple[int, str, str, str]] = []
    for path in _iter_backend_files():
        backend_findings.extend(_scan_file_with_patterns(path, BACKEND_ANTI_PATTERNS))

    backend_root = Path(__file__).resolve().parent.parent
    # Group findings by relative path
    by_file: dict[str, list[tuple[int, str, str, str]]] = {}
    for entry in backend_findings:
        line_no, match, file_path, msg = entry
        try:
            rel = str(Path(file_path).relative_to(backend_root))
        except ValueError:
            rel = str(file_path)
        by_file.setdefault(rel, []).append(entry)

    # Compare counts per file to the baseline.
    over_baseline: list[tuple[str, int, int]] = []
    new_files: list[str] = []
    for rel, entries in by_file.items():
        baseline = KNOWN_BACKEND_FINDINGS.get(rel)
        if baseline is None:
            new_files.append(rel)
        elif len(entries) > baseline:
            over_baseline.append((rel, len(entries), baseline))

    if over_baseline or new_files:
        report_lines = ["Backend canonical-table baseline violated:", ""]
        for rel, actual, baseline in over_baseline:
            report_lines.append(
                f"  {rel}: {actual} findings (baseline allows {baseline})"
            )
        for rel in new_files:
            report_lines.append(
                f"  {rel}: NEW — no baseline entry "
                f"({len(by_file[rel])} findings)"
            )
        report_lines.append("")
        report_lines.append(
            "If a NEW query SHOULD use the legacy `facilities` table, add "
            "a `# lint: legacy-facilities-ok` annotation explaining why. "
            "Otherwise switch to `discovered_facilities`. To accept new "
            "findings into the baseline (e.g. you added a file with known "
            "legacy queries that haven't been migrated), update "
            "KNOWN_BACKEND_FINDINGS in tests/test_canonical_fields.py."
        )
        raise AssertionError("\n".join(report_lines))


if __name__ == "__main__":
    # Allow running as `python tests/test_canonical_fields.py` for quick checks.
    exit_code = 0
    try:
        test_no_canonical_field_anti_patterns()
        print("OK: no canonical-field anti-patterns found in frontend.")
    except AssertionError as e:
        print(str(e))
        exit_code = 1
    try:
        test_no_backend_canonical_table_anti_patterns()
        print("OK: no canonical-table anti-patterns found in backend.")
    except AssertionError as e:
        print(str(e))
        exit_code = 1
    raise SystemExit(exit_code)
