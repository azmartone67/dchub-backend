#!/usr/bin/env python3
"""
DC Hub — Static Source Squasher (v7.9.10)
==========================================
Runs BEFORE deploy. Parses the Cloudflare Pages source tree and flags bug
classes that keep recurring in production. Designed to catch the 6 bug
families from the Apr-14 2026 QA pass:

  1. Dead/old Railway backend URLs baked into frontend
  2. Chart date-bucketing that defaults to Date.now() for undated rows
  3. Nav links that point to the wrong route (e.g. /ai vs /ai-integrations)
  4. XHR/fetch code that hits Railway cross-origin FIRST (CORS will fail)
  5. Static directory being shadowed by a _redirects rule
  6. Markets pages missing from /markets/index.html

Exit code 0 = all checks pass. 1 = issues found.
No external deps — stdlib only — so it runs on Replit, CI, or locally.

USAGE
-----
    python qa/squasher.py                 # scan current dir
    python qa/squasher.py /path/to/root   # scan specific root
    python qa/squasher.py --json          # machine-readable report
    python qa/squasher.py --fix           # auto-fix some classes (see code)

Add this to your Cloudflare Pages build command:
    python qa/squasher.py || (echo "Squasher failed — aborting deploy"; exit 1)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable


# ── Rules: each rule has an id, description, file glob, pattern, and a fix
#         hint. Rules that set `severity=blocker` fail the build.
KNOWN_DEAD_BACKENDS = (
    "web-production-e6382.up.railway.app",  # killed Mar 2026
    "dchub-api.up.railway.app",             # legacy (never production)
)

CANONICAL_BACKEND = "dchub-backend-production.up.railway.app"

# ── Nav-link canonical map (source of truth) ────────────────────────────────
# If the nav JS file references a label in this map, the href MUST match.
NAV_CANONICAL = {
    "AI Integration":    "/ai-integrations",
    "AI Integrations":   "/ai-integrations",
    "AI Hub":            "/ai",
    "AI Wars":           "/ai-wars",
    "Markets":           "/markets/",
    "Press Releases":    "/press",
    "GDCI":              "/gdci",
    "Assets Explorer":   "/assets",
    "API & MCP":         "/api-docs",
    "Developers":        "/developers",
}


@dataclass
class Finding:
    rule_id: str
    severity: str        # "blocker" | "warning" | "info"
    file: str
    line: int
    snippet: str
    message: str
    fix_hint: str = ""


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    def add(self, f: Finding) -> None:
        self.findings.append(f)

    @property
    def blockers(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "blocker"]

    def to_json(self) -> str:
        return json.dumps({"findings": [asdict(f) for f in self.findings]}, indent=2)


# ─── File walking ────────────────────────────────────────────────────────────
def iter_files(root: Path, exts: Iterable[str]) -> Iterable[Path]:
    skip_dirs = {".git", "node_modules", "dist", ".next", ".vercel", "__pycache__"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for fn in filenames:
            if any(fn.endswith(e) for e in exts):
                yield Path(dirpath) / fn


# ─── Rule R1: Dead Railway backends in frontend code ─────────────────────────
def rule_dead_backends(root: Path, report: Report) -> None:
    for fp in iter_files(root, (".html", ".js")):
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            for dead in KNOWN_DEAD_BACKENDS:
                if dead in line:
                    report.add(Finding(
                        rule_id="R1-DEAD-BACKEND",
                        severity="blocker",
                        file=str(fp.relative_to(root)),
                        line=i,
                        snippet=line.strip()[:200],
                        message=f"Dead Railway backend {dead!r} referenced in frontend.",
                        fix_hint=f"Remove it; use same-origin ('') or {CANONICAL_BACKEND}.",
                    ))


# ─── Rule R2: chart code that defaults to Date.now() on undated rows ─────────
# Catches: `new Date(item.foo || item.bar || Date.now())`
DATE_NOW_FALLBACK = re.compile(
    r"new\s+Date\(\s*[^)]*?\|\|\s*Date\.now\(\)\s*\)"
)

def rule_chart_date_nowfallback(root: Path, report: Report) -> None:
    for fp in iter_files(root, (".html", ".js")):
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if DATE_NOW_FALLBACK.search(line):
                report.add(Finding(
                    rule_id="R2-CHART-DATE-NOW",
                    severity="blocker",
                    file=str(fp.relative_to(root)),
                    line=i,
                    snippet=line.strip()[:200],
                    message="Chart falls back to Date.now() when date fields are missing — "
                            "undated rows will collapse into the current month.",
                    fix_hint="Skip undated rows or bucket them as 'TBD'. "
                             "See qa/README.md > R2 for pattern.",
                ))


# ─── Rule R3: Nav href drift (label/href mismatch in dchub-nav.js) ───────────
NAV_ITEM_RE = re.compile(
    r"""label\s*:\s*['"]([^'"]+)['"]\s*,\s*href\s*:\s*['"]([^'"]+)['"]"""
)

def rule_nav_href_drift(root: Path, report: Report) -> None:
    nav_js = root / "js" / "dchub-nav.js"
    if not nav_js.exists():
        return
    text = nav_js.read_text(encoding="utf-8", errors="replace")
    for i, line in enumerate(text.splitlines(), start=1):
        m = NAV_ITEM_RE.search(line)
        if not m:
            continue
        label, href = m.group(1), m.group(2)
        canonical = NAV_CANONICAL.get(label)
        if canonical and href != canonical:
            report.add(Finding(
                rule_id="R3-NAV-HREF-DRIFT",
                severity="blocker",
                file=str(nav_js.relative_to(root)),
                line=i,
                snippet=line.strip()[:200],
                message=f"Nav label {label!r} points to {href!r} but canonical is {canonical!r}.",
                fix_hint=f"Update href to {canonical!r} or remove label from NAV_CANONICAL.",
            ))


# ─── Rule R4: Frontend hits Railway cross-origin FIRST (CORS trap) ───────────
RAILWAY_FIRST_PATTERNS = (
    # var API_URL = 'https://*.railway.app/...'
    re.compile(r"""(?:var|let|const)\s+API_URL\s*=\s*['"]https?://[^'"]*railway\.app[^'"]*['"]"""),
    # fetch('https://*.railway.app/...') when no preceding same-origin attempt
    re.compile(r"""fetch\(\s*['"]https?://[^'"]*railway\.app[^'"]*['"]"""),
)

def rule_railway_first(root: Path, report: Report) -> None:
    for fp in iter_files(root, (".html", ".js")):
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            for pat in RAILWAY_FIRST_PATTERNS:
                if pat.search(line):
                    # allowlist: _worker.js obviously calls Railway.
                    if fp.name == "_worker.js":
                        continue
                    report.add(Finding(
                        rule_id="R4-CORS-TRAP",
                        severity="warning",
                        file=str(fp.relative_to(root)),
                        line=i,
                        snippet=line.strip()[:200],
                        message="Frontend calls Railway directly — CORS preflight will fail "
                                "without Allow-Credentials:true. Prefer same-origin ('/api/...') "
                                "which goes through the CF Worker proxy.",
                        fix_hint="Use '/api/...' as primary; keep Railway URL only as fallback.",
                    ))
                    break


# ─── Rule R5: _redirects rule shadowing a real static directory ──────────────
REDIRECT_RE = re.compile(r"^\s*(/[^\s]+)\s+(/[^\s]+)\s+(\d{3})\s*$")

def rule_redirects_shadow(root: Path, report: Report) -> None:
    redirects = root / "_redirects"
    if not redirects.exists():
        return
    lines = redirects.read_text(encoding="utf-8", errors="replace").splitlines()
    for i, raw in enumerate(lines, start=1):
        if raw.strip().startswith("#"):
            continue
        m = REDIRECT_RE.match(raw)
        if not m:
            continue
        src, dst, code = m.group(1), m.group(2), m.group(3)
        # If /foo/ or /foo matches a real directory with an index.html, the redirect shadows it.
        candidate_dir = root / src.strip("/")
        if candidate_dir.is_dir() and (candidate_dir / "index.html").exists() and code.startswith("3"):
            report.add(Finding(
                rule_id="R5-REDIRECT-SHADOW",
                severity="blocker",
                file="_redirects",
                line=i,
                snippet=raw.strip()[:200],
                message=f"Redirect {src!r} → {dst!r} ({code}) hides the real directory "
                        f"{candidate_dir.relative_to(root)}/ which has an index.html.",
                fix_hint="Delete the redirect or rename the static directory.",
            ))


# ─── Rule R6: markets/index.html lists every market page in markets/ ─────────
def rule_markets_index_completeness(root: Path, report: Report) -> None:
    mdir = root / "markets"
    if not mdir.is_dir():
        return
    index = mdir / "index.html"
    if not index.exists():
        report.add(Finding(
            rule_id="R6-MARKETS-NO-INDEX",
            severity="blocker",
            file="markets/",
            line=0,
            snippet="",
            message="No markets/index.html — /markets/ will 404.",
            fix_hint="Create a markets/index.html listing each city page.",
        ))
        return
    idx_text = index.read_text(encoding="utf-8", errors="replace")
    pages = [p.stem for p in mdir.glob("*.html") if p.name != "index.html"]
    missing = sorted([p for p in pages if f'"{p}"' not in idx_text and f"/{p}" not in idx_text])
    if missing:
        report.add(Finding(
            rule_id="R6-MARKETS-INDEX-STALE",
            severity="warning",
            file="markets/index.html",
            line=0,
            snippet=", ".join(missing[:10]) + ("…" if len(missing) > 10 else ""),
            message=f"{len(missing)} market page(s) exist but are not linked from markets/index.html.",
            fix_hint="Add a <a href=\"{slug}\" class=\"market-card\">…</a> block for each missing page.",
        ))


# ─── Rule R7: index.html markets banner has stale slugs ──────────────────────
MARKET_PILL_SLUG_RE = re.compile(r"slug\s*:\s*['\"]([a-z0-9\-]+)['\"]")

def rule_front_banner_slugs(root: Path, report: Report) -> None:
    index = root / "index.html"
    mdir = root / "markets"
    if not index.exists() or not mdir.is_dir():
        return
    text = index.read_text(encoding="utf-8", errors="replace")
    slugs = MARKET_PILL_SLUG_RE.findall(text)
    for slug in slugs:
        if not (mdir / f"{slug}.html").exists():
            report.add(Finding(
                rule_id="R7-FRONT-BANNER-STALE",
                severity="blocker",
                file="index.html",
                line=0,
                snippet=f"slug: '{slug}'",
                message=f"Front-page markets banner references /markets/{slug} but no "
                        f"markets/{slug}.html exists.",
                fix_hint=f"Create markets/{slug}.html or drop the pill from index.html.",
            ))


RULES = [
    ("R1", rule_dead_backends),
    ("R2", rule_chart_date_nowfallback),
    ("R3", rule_nav_href_drift),
    ("R4", rule_railway_first),
    ("R5", rule_redirects_shadow),
    ("R6", rule_markets_index_completeness),
    ("R7", rule_front_banner_slugs),
]


# ─── Auto-fix (opt-in) ───────────────────────────────────────────────────────
def auto_fix(root: Path, report: Report) -> int:
    """Applies safe fixes only. Returns number of files modified."""
    fixed = 0
    for f in report.findings:
        if f.rule_id == "R1-DEAD-BACKEND":
            fp = root / f.file
            text = fp.read_text(encoding="utf-8")
            for dead in KNOWN_DEAD_BACKENDS:
                # Rip out list entries like  'https://...',
                text = re.sub(
                    r"""['"]https?://""" + re.escape(dead) + r"""[^'"]*['"],?\s*""",
                    "",
                    text,
                )
            fp.write_text(text, encoding="utf-8")
            fixed += 1
    return fixed


# ─── CLI ─────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="DC Hub static-source bug squasher")
    p.add_argument("root", nargs="?", default=".", help="Project root")
    p.add_argument("--json", action="store_true", help="Emit JSON report")
    p.add_argument("--fix", action="store_true", help="Apply safe auto-fixes")
    p.add_argument("--strict", action="store_true", help="Warnings also fail build")
    args = p.parse_args(argv)

    root = Path(args.root).resolve()
    if not root.exists():
        print(f"Root not found: {root}", file=sys.stderr)
        return 2

    report = Report()
    for rid, fn in RULES:
        try:
            fn(root, report)
        except Exception as exc:
            report.add(Finding(rule_id=f"{rid}-CRASH", severity="warning",
                               file="", line=0, snippet="",
                               message=f"Rule {rid} crashed: {exc}"))

    if args.fix:
        fixed = auto_fix(root, report)
        print(f"Auto-fix: modified {fixed} file(s). Re-run without --fix to verify.")

    if args.json:
        print(report.to_json())
    else:
        if not report.findings:
            print("✓ Squasher: clean. No findings.")
        else:
            print(f"Squasher findings ({len(report.findings)}):")
            for f in report.findings:
                tag = {"blocker": "✗", "warning": "!", "info": "i"}[f.severity]
                where = f"{f.file}:{f.line}" if f.line else f.file
                print(f"  {tag} [{f.rule_id}] {where}")
                print(f"      {f.message}")
                if f.fix_hint:
                    print(f"      → {f.fix_hint}")

    fails = report.blockers + ([f for f in report.findings if f.severity == "warning"] if args.strict else [])
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
