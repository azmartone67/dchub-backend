#!/usr/bin/env python3
"""r48.2 (2026-05-27) — bug-squash: preemptive bug-class detection.

What this is
============
A pattern scanner that codifies every bug CLASS the user has had to
report twice. Each pattern detector knows:

  1. The bug shape (regex / AST check)
  2. Why it's bad (links to the original incident)
  3. The canonical fix
  4. Whether it should block CI (`severity = blocker|critical`) or just
     surface in the weekly digest (`important|polish`)

When a NEW kind of bug eats two cycles, we ADD a pattern to PATTERNS
below. Future PRs that reintroduce it fail at the gate; the brain's
weekly digest surfaces existing instances for cleanup.

Modes
=====
    python scripts/bug_squash.py                # text report, exit 1 if blockers
    python scripts/bug_squash.py --format json  # machine-readable
    python scripts/bug_squash.py --file-to-brain  # POST to /api/v1/brain/bug-squash/file
    python scripts/bug_squash.py --pattern hardcoded_hero  # one detector only

Brain integration
=================
The companion brain route `routes/brain_bug_squash.py` provides:
  POST /api/v1/brain/bug-squash/run       — trigger a scan, persist findings
  GET  /api/v1/brain/bug-squash/findings  — list open findings
  POST /api/v1/brain/bug-squash/resolve   — mark a finding resolved

A GitHub Actions cron (workflow: bug-squash-nightly.yml) fires this
every day at 04:00 UTC; new findings show up as brain_findings rows
and surface in the inspector_brief + weekly digest.

Adding a new pattern
====================
1. Subclass BugPattern.
2. Implement detect(file_text, file_path) → list[Finding].
3. Register in PATTERNS.
4. Add a smoke test that re-runs against the file you fixed today
   and asserts the count matches the baseline (or zero).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Iterable, Optional

BACKEND_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_ROOT = Path(os.environ.get(
    "DCHUB_FRONTEND_ROOT",
    str(BACKEND_ROOT.parent / "dchub-frontend"),
))


# ──────────────────────────────────────────────────────────────────────
# Data shapes
# ──────────────────────────────────────────────────────────────────────
@dataclass
class Finding:
    pattern_id: str
    severity: str  # blocker|critical|important|polish
    file: str
    line: int
    snippet: str
    why: str
    suggested_fix: str
    reference: str = ""  # commit / PR / runbook URL

    def to_brain_finding(self) -> dict:
        """Shape expected by brain_findings table."""
        return {
            "issue": f"bug_squash:{self.pattern_id}",
            "url": f"{self.file}#L{self.line}",
            "count": 1,
            "detail": (
                f"{self.why}\n\n"
                f"Snippet: {self.snippet}\n\n"
                f"Suggested fix: {self.suggested_fix}\n\n"
                f"Reference: {self.reference}"
            )[:2000],
            "detector": "brain_bug_squash",
            "status": "open",
        }


@dataclass
class BugPattern:
    """Base class. Subclass and override detect().

    Attributes:
        id          stable slug used in CI assertions + brain_findings
        severity    blocker / critical / important / polish
        title       one-line description (for digests)
        why         multi-line explanation (the incident that motivated it)
        reference   commit URL or runbook link
        files_glob  which files to scan ("**/*.html", "routes/*.py" etc)
        roots       which roots to glob from (FRONTEND_ROOT, BACKEND_ROOT)
    """
    id: str
    severity: str
    title: str
    why: str
    reference: str
    files_glob: tuple[str, ...]
    roots: tuple[Path, ...]

    # Files where bug patterns appear AS EXAMPLES (their detector source
    # or their test fixtures). Skip to avoid self-referential matches.
    SELF_EXCLUDE = {
        "bug_squash.py",
        "test_canonical_fields.py",
        "test_anchor_card_clickable.py",
        "test_pro_paywall_visibility.py",
        "brain_bug_squash.py",
    }

    def iter_files(self) -> Iterable[Path]:
        for root in self.roots:
            if not root.exists():
                continue
            for glob in self.files_glob:
                for path in root.rglob(glob):
                    if any(seg in path.parts for seg in
                           ("node_modules", "dist", "build", ".git",
                            "__pycache__")):
                        continue
                    if path.name in self.SELF_EXCLUDE:
                        continue
                    yield path

    def detect(self, text: str, path: Path) -> list[Finding]:
        raise NotImplementedError


# ──────────────────────────────────────────────────────────────────────
# Shared helpers — comment-aware regex matching used by many patterns
# ──────────────────────────────────────────────────────────────────────
def is_comment_line(line: str) -> bool:
    s = line.lstrip()
    return s.startswith(("#", "//", "/*", "*", "<!--"))


def in_comment(line: str, match_start_in_line: int) -> bool:
    if is_comment_line(line):
        return True
    # Inline `//` or `#` BEFORE the match
    for marker in ("//", "#"):
        idx = line.find(marker)
        if 0 <= idx < match_start_in_line:
            return True
    # Inside backtick-quoted string
    if line[:match_start_in_line].count("`") % 2 == 1:
        return True
    return False


def line_no_for(text: str, offset: int) -> int:
    return text[:offset].count("\n") + 1


# ──────────────────────────────────────────────────────────────────────
# PATTERN 1 — Hardcoded hero stats (the 13,000+ / 21,000+ / 140+ class)
# ──────────────────────────────────────────────────────────────────────
class HardcodedHeroNumberPattern(BugPattern):
    """A static literal like '13,000+' or '$324B' appearing inside a
    hero/stat element that has an id (i.e. it's MEANT to be hydrated).
    If the JS hydrator silently breaks (see r48.0 about.html), the page
    keeps showing the stale literal.

    Detection: <element id="..." ... >13,000+</element> where the
    element ID matches a known-hydrated naming pattern, OR the literal
    is a number-plus-suffix.

    Strategy: catch the LITERAL between tags, not the whole element.
    """
    _STATIC_RE = re.compile(
        r'id=["\'](?P<id>[a-z][a-z0-9-]*(?:stat|count|fac|kpi|hero|num|gw|mw|mc)[a-z0-9-]*)["\']'
        r'(?P<rest>[^>]*)>'
        r'(?P<literal>\s*[\$]?\d[\d,.]*\s*[KMBT]?\+?\s*(?:GW|MW|kWh|deals?|countries?|markets?|states?|facilities|years?)?\s*)<',
        re.IGNORECASE,
    )
    # Values that are clearly OK (placeholders, dashes)
    _PLACEHOLDER = {"—", "-", "", "0", "N/A", "?"}

    def detect(self, text: str, path: Path) -> list[Finding]:
        out: list[Finding] = []
        for m in self._STATIC_RE.finditer(text):
            literal = m.group("literal").strip()
            if not literal or literal in self._PLACEHOLDER:
                continue
            # Skip if the value contains a Jinja/template placeholder
            if any(tok in literal for tok in ("{", "}", "$", "${")):
                continue
            line = line_no_for(text, m.start())
            out.append(Finding(
                pattern_id=self.id,
                severity=self.severity,
                file=str(path),
                line=line,
                snippet=m.group(0)[:140],
                why=(
                    "Element has an id that suggests it's meant to be hydrated "
                    "from /api/v1/stats (or similar), but the static literal "
                    f"`{literal}` is what users see if the hydrator silently "
                    "breaks. Today's about.html 13,000+→21,433 fix was exactly "
                    "this class. The literal stays visible forever when the "
                    "fetch fails or parses the wrong field."
                ),
                suggested_fix=(
                    "Either (a) leave the literal as a placeholder '—' so a "
                    "broken hydrator is obvious, or (b) confirm the hydrator "
                    "test in tests/test_canonical_fields.py covers the field "
                    "this element binds to."
                ),
                reference=self.reference,
            ))
        return out


# ──────────────────────────────────────────────────────────────────────
# PATTERN 2 — JS field-fallback missing (the r.operator/r.company class)
# ──────────────────────────────────────────────────────────────────────
class JsFieldFallbackMissingPattern(BugPattern):
    """`item.X` (or `r.X`, `row.X`) read WITHOUT a `|| item.Y` fallback
    when X and Y are known synonyms in the API surface.

    Today's incident: capacity-pipeline.html read `item.operator` in
    renderTable but the API returns rows keyed on `company`. The
    aggregation block above had the fallback; the render block didn't.

    Detection: simple keyword pair check over .js / .html.
    """
    # r48.2 (2026-05-27): start with just the (operator, company) pair —
    # the exact shape that caused today's /capacity-pipeline "Unknown"
    # bug. The other pairs (market/city, state/region, provider/operator)
    # produce too much noise from legitimate single-field reads. Add a
    # pair here ONLY after the user reports a second incident in that
    # shape; until then it's just speculative.
    _PAIRS = [
        ("operator", "company"),
    ]

    def detect(self, text: str, path: Path) -> list[Finding]:
        out: list[Finding] = []
        for primary, fallback in self._PAIRS:
            # Look for accesses like `item.operator`, `row.operator`, `r.operator`
            access_re = re.compile(
                rf"\b(item|row|r|d|x)\.{primary}\b(?!\s*\|\|\s*\1\.{fallback}\b)"
            )
            for m in access_re.finditer(text):
                line_no = line_no_for(text, m.start())
                lines = text.splitlines()
                if not (0 < line_no <= len(lines)):
                    continue
                line = lines[line_no - 1]
                idx_in_line = line.find(m.group(0))
                if in_comment(line, idx_in_line):
                    continue
                # If the same line already has the fallback elsewhere, OK.
                if re.search(rf"\.{fallback}\b", line):
                    continue
                # Common false-positive: assignment like `item.operator = ...`
                rest = line[idx_in_line + len(m.group(0)):].lstrip()
                if rest.startswith("="):
                    continue
                out.append(Finding(
                    pattern_id=self.id,
                    severity=self.severity,
                    file=str(path),
                    line=line_no,
                    snippet=line.strip()[:160],
                    why=(
                        f"Reads `{m.group(0)}` without falling back to "
                        f"`{m.group(1)}.{fallback}`. The /api/pipeline (and "
                        "similar endpoints) return rows keyed on one or the "
                        "other depending on source — readers should accept "
                        "either. Same class as today's capacity-pipeline "
                        "'Unknown' bug (r48.1)."
                    ),
                    suggested_fix=(
                        f"Change `{m.group(0)}` → "
                        f"`{m.group(1)}.{primary} || {m.group(1)}.{fallback} "
                        "|| ''`"
                    ),
                    reference=self.reference,
                ))
        return out


# ──────────────────────────────────────────────────────────────────────
# PATTERN 3 — SQL GROUP BY duplicate-risk (the "Northern Virginia twice"
# class from quarterly_report)
# ──────────────────────────────────────────────────────────────────────
class SqlGroupByDuplicatePattern(BugPattern):
    """SELECT COALESCE(a, b) ... GROUP BY a, b — two-column grouping
    when the displayed value is the COALESCE single column. Produces
    one row per (a, b) tuple, so the same display value shows N times.

    Today's incident: quarterly_report top_markets had
    `SELECT COALESCE(market, city) ... GROUP BY market, city` →
    Northern Virginia appeared twice.
    """
    _COALESCE_SELECT = re.compile(
        r"SELECT[^;]*?COALESCE\((?P<a>\w+)\s*,\s*(?P<b>\w+)(?:\s*,\s*[^)]*)?\)",
        re.IGNORECASE | re.DOTALL,
    )
    _MULTI_COL_GROUP = re.compile(
        r"GROUP\s+BY\s+(?P<cols>[a-z_,\s]+)",
        re.IGNORECASE,
    )

    def detect(self, text: str, path: Path) -> list[Finding]:
        out: list[Finding] = []
        # Find each COALESCE select + its enclosing query, check the GROUP BY.
        for m in self._COALESCE_SELECT.finditer(text):
            a, b = m.group("a"), m.group("b")
            # Look ahead up to 2000 chars for the GROUP BY in the same query.
            chunk = text[m.start():m.start() + 2000]
            gm = self._MULTI_COL_GROUP.search(chunk)
            if not gm:
                continue
            cols = [c.strip() for c in gm.group("cols").split(",")]
            # The bug is: SELECT COALESCE(a, b), GROUP BY a, b
            if a in cols and b in cols:
                line_no = line_no_for(text, m.start() + gm.start())
                lines = text.splitlines()
                line = lines[line_no - 1] if 0 < line_no <= len(lines) else ""
                if is_comment_line(line):
                    continue
                out.append(Finding(
                    pattern_id=self.id,
                    severity=self.severity,
                    file=str(path),
                    line=line_no,
                    snippet=f"SELECT COALESCE({a}, {b}) ... GROUP BY {a}, {b}",
                    why=(
                        f"Query selects `COALESCE({a}, {b})` as the displayed "
                        f"value but groups by `({a}, {b})`. The grouping "
                        "creates one row per tuple, so the same display value "
                        "appears multiple times when the underlying fields "
                        "differ. Today's quarterly_report 'Northern Virginia "
                        "twice' bug (r48.1)."
                    ),
                    suggested_fix=(
                        f"`GROUP BY COALESCE({a}, {b}, '')` (matches the "
                        "SELECT display) instead of `GROUP BY {a}, {b}`."
                    ),
                    reference=self.reference,
                ))
        return out


# ──────────────────────────────────────────────────────────────────────
# PATTERN 4 — Frontend canonical-field reads (delegates to the existing
# test_canonical_fields.py logic so we don't duplicate it)
# ──────────────────────────────────────────────────────────────────────
class FrontendCanonicalFieldPattern(BugPattern):
    """Wraps tests/test_canonical_fields.py:ANTI_PATTERNS so the same
    logic runs from bug_squash too. Single source of truth lives in the
    test file."""

    def detect(self, text: str, path: Path) -> list[Finding]:
        # Lazy import so test running this script doesn't fail in CI.
        try:
            from tests.test_canonical_fields import (
                ANTI_PATTERNS, _scan_file_with_patterns,
            )
        except Exception:
            return []
        # _scan_file_with_patterns operates on a Path, so re-read.
        raw_findings = _scan_file_with_patterns(path, ANTI_PATTERNS)
        out: list[Finding] = []
        for line_no, match, _file, msg in raw_findings:
            out.append(Finding(
                pattern_id=self.id,
                severity=self.severity,
                file=str(path),
                line=line_no,
                snippet=match[:160],
                why=msg,
                suggested_fix=(
                    "See message — typically: prefer the canonical field "
                    "(`total_facilities`, `total_countries`, `d.data`) "
                    "first in the read chain."
                ),
                reference=self.reference,
            ))
        return out


# ──────────────────────────────────────────────────────────────────────
# PATTERN 5 — Anchor-card click-region (delegates to test_anchor_card_clickable.py)
# ──────────────────────────────────────────────────────────────────────
class AnchorCardClickRegionPattern(BugPattern):
    """Wraps tests/test_anchor_card_clickable.py's logic."""

    def detect(self, text: str, path: Path) -> list[Finding]:
        try:
            from tests.test_anchor_card_clickable import _scan_file, OPT_OUT
        except Exception:
            return []
        if path.name in OPT_OUT:
            return []
        findings = _scan_file(path)
        if not findings:
            return []
        out: list[Finding] = []
        for msg in findings:
            out.append(Finding(
                pattern_id=self.id,
                severity=self.severity,
                file=str(path),
                line=0,  # file-level finding
                snippet="<a class=\"card\"> or <a class=\"card-link\">",
                why=msg,
                suggested_fix=(
                    "Add `display:block;text-decoration:none;color:inherit` "
                    "to the relevant `.card` or `.card-link` CSS rule. See "
                    "r47.44 (dcpi.py) / r47.45 (dcgi.py) / r47.46 "
                    "(operators.py + competitive_seo.py)."
                ),
                reference=self.reference,
            ))
        return out


# ──────────────────────────────────────────────────────────────────────
# PATTERN REGISTRY
# ──────────────────────────────────────────────────────────────────────
PATTERNS: list[BugPattern] = [
    HardcodedHeroNumberPattern(
        id="hardcoded_hero_stat",
        severity="polish",  # not blocking — may be intentional, just want visibility
        title="Hardcoded hero stat that should hydrate",
        why=(
            "Static literals like '13,000+' inside an element with an id "
            "are MEANT to be replaced at runtime by /api/v1/stats fetcher. "
            "When the hydrator silently breaks, users see the stale literal. "
            "About.html 13k→21,433 (r48.0) was this."
        ),
        reference="https://github.com/azmartone67/dchub-backend/commit/r48.0",
        files_glob=("*.html",),
        roots=(FRONTEND_ROOT,),
    ),
    JsFieldFallbackMissingPattern(
        id="js_field_fallback_missing",
        severity="important",
        title="JS reads X without || Y fallback when API returns either",
        why=(
            "API endpoints don't always agree on field names "
            "(operator vs company, market vs city). When the consumer "
            "code reads only one, half the rows go missing or show "
            "'Unknown'. Today's capacity-pipeline operators=Unknown "
            "across the board (r48.1) was this."
        ),
        reference="https://github.com/azmartone67/dchub-backend/commit/r48.1",
        files_glob=("*.html", "*.js"),
        roots=(FRONTEND_ROOT,),
    ),
    SqlGroupByDuplicatePattern(
        id="sql_groupby_coalesce_duplicate",
        severity="critical",
        title="SELECT COALESCE(a,b) with GROUP BY a, b → duplicates",
        why=(
            "Selecting `COALESCE(a, b)` as a display value while grouping "
            "by `(a, b)` produces one row per (a, b) tuple. Same display "
            "value shows N times. Today's quarterly_report 'Northern "
            "Virginia twice' (r48.1) was this."
        ),
        reference="https://github.com/azmartone67/dchub-backend/commit/r48.1",
        files_glob=("*.py",),
        roots=(BACKEND_ROOT / "routes", BACKEND_ROOT),
    ),
    FrontendCanonicalFieldPattern(
        id="frontend_canonical_field",
        severity="blocker",
        title="Frontend reads deprecated/wrong canonical field",
        why=(
            "Reading `main_facilities` first (12,907 legacy) or "
            "`d.stats || d` (the envelope shape is `{data: {...}}`) "
            "produces stale numbers in hero stats. Already fenced by "
            "tests/test_canonical_fields.py; this surfaces the count "
            "for the brain digest."
        ),
        reference="https://github.com/azmartone67/dchub-backend/commit/r48.0",
        files_glob=("*.html", "*.js"),
        roots=(FRONTEND_ROOT,),
    ),
    AnchorCardClickRegionPattern(
        id="anchor_card_click_region",
        severity="critical",
        title="<a class=\"card\"> without display:block",
        why=(
            "Default-inline anchors collapse to ~0px hit region in CSS "
            "Grid. Cards look clickable but clicks land on the inner "
            "div with no href. Same class fixed in r47.44/r47.45/r47.46 "
            "across 5 routes today."
        ),
        reference="https://github.com/azmartone67/dchub-backend/commit/r47.44",
        files_glob=("*.py",),
        roots=(BACKEND_ROOT / "routes",),
    ),
]


# ──────────────────────────────────────────────────────────────────────
# Orchestrator
# ──────────────────────────────────────────────────────────────────────
class BugSquash:
    def __init__(self, patterns: list[BugPattern] | None = None):
        self.patterns = patterns if patterns is not None else PATTERNS

    def scan(self, pattern_filter: str | None = None) -> list[Finding]:
        findings: list[Finding] = []
        for p in self.patterns:
            if pattern_filter and p.id != pattern_filter:
                continue
            for path in p.iter_files():
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                findings.extend(p.detect(text, path))
        return findings

    @staticmethod
    def report_text(findings: list[Finding]) -> str:
        if not findings:
            return "OK — no bug-squash findings."
        out = [f"Found {len(findings)} bug-squash finding(s):", ""]
        by_severity = {}
        for f in findings:
            by_severity.setdefault(f.severity, []).append(f)
        for sev in ("blocker", "critical", "important", "polish"):
            for f in by_severity.get(sev, []):
                try:
                    rel = Path(f.file).relative_to(BACKEND_ROOT)
                except ValueError:
                    try:
                        rel = Path(f.file).relative_to(FRONTEND_ROOT)
                    except ValueError:
                        rel = Path(f.file)
                out.append(f"  [{sev}] {f.pattern_id}")
                out.append(f"    {rel}:{f.line}")
                out.append(f"    {f.snippet}")
                out.append(f"    why: {f.why[:200]}")
                out.append(f"    fix: {f.suggested_fix[:200]}")
                out.append("")
        return "\n".join(out)

    @staticmethod
    def report_json(findings: list[Finding]) -> str:
        return json.dumps([asdict(f) for f in findings], indent=2,
                          ensure_ascii=False)

    def has_blockers(self, findings: list[Finding]) -> bool:
        return any(f.severity == "blocker" for f in findings)


def file_findings_to_brain(findings: list[Finding], base_url: str) -> dict:
    """POST findings to /api/v1/brain/bug-squash/file (handled by
    routes/brain_bug_squash.py). Returns the API response."""
    if not findings:
        return {"ok": True, "count": 0}
    try:
        import urllib.request
    except ImportError:
        return {"ok": False, "error": "no urllib"}
    payload = {"findings": [f.to_brain_finding() for f in findings]}
    data = json.dumps(payload).encode("utf-8")
    admin_key = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY") or ""
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/v1/brain/bug-squash/file",
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Admin-Key": admin_key,
            "User-Agent": "dchub-bug-squash/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8", errors="ignore"))
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description="bug-squash: preemptive bug-class scanner")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--pattern", help="Run only this pattern id")
    parser.add_argument("--file-to-brain", action="store_true",
                        help="POST findings to /api/v1/brain/bug-squash/file")
    parser.add_argument("--brain-url", default=os.environ.get(
        "DCHUB_BRAIN_URL",
        "https://dchub-backend-production.up.railway.app",
    ))
    parser.add_argument("--fail-on", choices=("blocker", "any", "never"),
                        default="blocker")
    args = parser.parse_args()

    sq = BugSquash()
    findings = sq.scan(pattern_filter=args.pattern)

    if args.format == "json":
        print(sq.report_json(findings))
    else:
        print(sq.report_text(findings))

    if args.file_to_brain:
        result = file_findings_to_brain(findings, args.brain_url)
        print(json.dumps({"brain_file_result": result}, indent=2))

    if args.fail_on == "blocker" and sq.has_blockers(findings):
        return 1
    if args.fail_on == "any" and findings:
        return 1
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(BACKEND_ROOT))
    raise SystemExit(main())
