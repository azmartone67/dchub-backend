"""tests/test_facility_stock_is_distinct.py — a served page must not publish
raw source ROWS as its facility total (2026-09-04).

WHAT HAPPENED. Measured live 2026-09-04 against canon facilities_distinct =
20,352, nine served pages published the row pile as "facilities":

    28,251  /coverage /sample /sample/agent /sample/journalist
            /reports/monthly /reports/quarterly-deep
    28,250  /sample/pe
    21,405  /partners/gemini            (a hardcoded literal)
    18,000  /land-power                 (frontend, stale under-claim)

/coverage also published "25 countries" against 178 — it was rendering
len(by_country), and by_country carries LIMIT 25 for its display table.

WHY THE EXISTING GUARDS MISSED IT.

  · tests/test_facility_claim_guard.py catches exactly this rows-as-buildings
    error — the 2026-08-17 "26,000 facilities" LinkedIn post — but it reads
    OUTBOUND POST TEXT through routes.media_fact_check_guard. No guard reads
    the Flask routes, so a <title> computing the same wrong number shipped and
    stayed.
  · tests/test_canon_placeholders_resolved.py walks the AST for {canon_*}
    placeholders that escaped canon_text(). /partners/gemini's hero had no
    placeholder to find — just the literal "21,405+". A guard that checks
    placeholders resolve cannot see a number that was never a placeholder.
  · scripts/accuracy_fence.py (frontend repo) scans REPO FILES, and every one
    of these pages is backend-served.

THE RULE. `COUNT(*) FROM discovered_facilities` is a legitimate query — it is
the record count, and `facilities_records` publishes it under that name. It is
NOT the facility STOCK. Any variable or dict key naming itself a facility total
must be computed with COUNT(DISTINCT canonical_slug), mirroring the citable
field in public_endpoints.py.

Run:  python3 -m pytest tests/test_facility_stock_is_distinct.py -v
"""
from __future__ import annotations

import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Names that mean "how many facilities are there" — a STOCK, not a flow.
# Deliberately excludes *_added, *_records, *_rows, *_new: those are flows or
# are explicitly labelled as rows, and COUNT(*) is right for them.
STOCK_NAME = re.compile(
    r"\b(total_facilities|facilities_total|facilities_now|facility_total|facilities_count)\b"
)
# ★ SQL in this repo is written as adjacent Python string literals:
#       "SELECT COUNT(DISTINCT canonical_slug) "
#       "FROM discovered_facilities "
# One statement to Postgres, two lines to a regex. GAP lets a token boundary
# cross a concat seam, so the guard can see the query it demands WITHOUT
# rewriting the text — an earlier version joined the seams instead and
# destroyed the line numbers it reports.
GAP = r'(?:\s|["\']\s*\n?\s*["\'])*'
RAW_COUNT = re.compile(
    r"COUNT\(" + GAP + r"\*" + GAP + r"\)" + GAP + r"FROM" + GAP + r"discovered_facilities",
    re.I)
DISTINCT_COUNT = re.compile(
    r"COUNT\(" + GAP + r"DISTINCT" + GAP + r"canonical_slug" + GAP + r"\)" + GAP
    + r"FROM" + GAP + r"discovered_facilities",
    re.I)

# Files that serve public pages or the canonical API.
def _scan_files():
    out = []
    for rel in ("main.py", "public_endpoints.py", "canonical_stats.py"):
        p = os.path.join(REPO, rel)
        if os.path.isfile(p):
            out.append(p)
    rdir = os.path.join(REPO, "routes")
    for fn in sorted(os.listdir(rdir)):
        if fn.endswith(".py") and not fn.startswith("_proposed_"):
            out.append(os.path.join(rdir, fn))
    return out


# Built from chr() so this guard file never contains a bare triple quote that
# its own docstring-stripper would then have to reason about.
_TQ = chr(34) * 3
_SQ = chr(39) * 3
TQ_PAT = _TQ + r"(?:.|\n)*?" + _TQ
SQ_PAT = _SQ + r"(?:.|\n)*?" + _SQ


def _blank_keep_lines(match):
    """Replace a matched block with the SAME number of newlines.

    ★ The first version of this guard deleted docstrings with a plain
    re.sub(). That collapses lines, so every reported line number was shifted
    and pointed at innocent code — it named a /testimonials route in main.py
    for a violation elsewhere. A scanner that reports the wrong location is
    worse than none: it sends the reader to code that is fine."""
    return "\n" * match.group(0).count("\n")


def _normalize(text):
    """Strip prose, KEEP line numbers, and join Python string-concat seams.

    Two things this must get right:
      1. Line count is preserved (see _blank_keep_lines).
      2. Text is otherwise left ALONE. Concat seams are handled by GAP in the
         regexes above, not by rewriting here — joining them was how an earlier
         version lost the line numbers it reports."""
    text = re.sub(TQ_PAT, _blank_keep_lines, text)
    text = re.sub(SQ_PAT, _blank_keep_lines, text)
    text = "\n".join(re.sub(r"#.*$", "", ln) for ln in text.split("\n"))
    return text


# How a raw count reaches a published total, in the two shapes this repo uses:
#
#   (1) direct    facilities_now = int(_safe_scalar(cur, "SELECT COUNT(*) ..."))
#   (2) two-step  cur.execute("SELECT COUNT(*) ...")
#                 out["total_facilities"] = cur.fetchone()[0]
#
# ★ Only these count. An earlier version flagged anything with a stock NAME
# within 4 lines of a raw count, which accused correct code: facilities_by_dims
# sets total_facilities from facilities_distinct and then, two lines later,
# queries COUNT(*) for facilities_records — properly labelled, and exactly what
# facilities_records means. A guard that cries wolf on the canonical endpoint
# is a guard people learn to skip.
ASSIGN_LOOKAHEAD = 3  # lines from an execute() to the fetch that consumes it
ASSIGN = re.compile(
    r"""(?:^|\W)(?:                      # a stock-named assignment target
            (?P<bare>total_facilities|facilities_total|facilities_now
                     |facility_total|facilities_count)
          | \[\s*["'](?P<key>total_facilities|facilities_total|facilities_now
                              |facility_total|facilities_count)["']\s*\]
        )\s*=""",
    re.X,
)
FETCH = re.compile(r"fetchone|fetchall|_safe_scalar|safe_query|scalar")


def test_facility_stock_never_uses_raw_row_count():
    violations = []
    for path in _scan_files():
        try:
            raw = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        if "discovered_facilities" not in raw:
            continue
        lines = _normalize(raw).split("\n")
        for i, line in enumerate(lines):
            # A raw count can span a concat seam, so test a small joined slab —
            # but the match must BEGIN on line i, or a query on the next line
            # gets attributed to this one. That mis-attribution flagged
            #   facilities_count = safe_query("SELECT COUNT(*) FROM facilities")
            # (the LEGACY table, correctly labelled record_count) because the
            # line below it happened to query discovered_facilities.
            slab = "\n".join(lines[i:i + 2])
            m = RAW_COUNT.search(slab)
            if not m or m.start() > len(line):
                continue
            if DISTINCT_COUNT.search(slab):
                continue
            # (1) same-statement assignment
            hit = bool(ASSIGN.search(line))
            # (2) execute(...) here, stock-named fetch just below
            if not hit:
                for j in range(i + 1, min(len(lines), i + 1 + ASSIGN_LOOKAHEAD)):
                    nxt = lines[j]
                    if ASSIGN.search(nxt) and FETCH.search(nxt):
                        hit = True
                        break
                    if RAW_COUNT.search(nxt) or DISTINCT_COUNT.search(nxt):
                        break  # a new query owns the lines below
            if hit:
                violations.append(
                    f"{os.path.relpath(path, REPO)}:{i + 1} — a facility STOCK is "
                    f"filled from COUNT(*) (source rows, ~1.4x buildings). Use "
                    f"COUNT(DISTINCT canonical_slug) ... WHERE canonical_slug IS NOT NULL."
                )
    assert not violations, (
        "Raw source ROWS published as a facility total — the 2026-09-04 defect "
        "(/coverage, /reports/*, /sample*) and the 2026-08-17 post before it:\n  "
        + "\n  ".join(violations)
    )


def test_the_pages_that_regressed_now_use_distinct():
    """Pin the four call sites the 2026-09-04 fix touched.

    The scan above is name-based and would go quiet if someone renamed a
    variable. These assert the specific modules still compute a distinct count
    at all, so a rename cannot silently disarm the guard."""
    expected = [
        "routes/coverage_page.py",
        "routes/monthly_trend.py",
        "routes/comprehensive_report.py",
        "public_endpoints.py",
    ]
    missing = []
    for rel in expected:
        p = os.path.join(REPO, rel)
        if not os.path.isfile(p):
            missing.append(f"{rel} — file is gone; re-point this guard")
            continue
        src = _normalize(open(p, encoding="utf-8", errors="replace").read())
        if not DISTINCT_COUNT.search(src):
            missing.append(f"{rel} — no COUNT(DISTINCT canonical_slug) left in this file")
    assert not missing, "facility-stock call sites regressed:\n  " + "\n  ".join(missing)


def test_coverage_countries_is_not_a_display_limit():
    """/coverage published `25 countries` because countries_tracked was
    len(by_country), and by_country is LIMIT 25 for the table it renders."""
    p = os.path.join(REPO, "routes", "coverage_page.py")
    # ★ Normalise BOTH sides. The first version of this assertion stripped
    # spaces from the source and left them in the needle, so it could never
    # match — it passed against the very defect it names. Mutation-checked:
    # restoring `len(out["by_country"])` must fail this test.
    src = re.sub(r"\s+", "", _normalize(open(p, encoding="utf-8", errors="replace").read()))
    needle = re.sub(r"\s+", "", 'countries_tracked"] = len(')
    assert needle not in src, (
        "coverage_page.countries_tracked is being set from len() of a LIMIT-ed "
        "display list again — that is how the page published \"25 countries\" "
        "against a canon of 178. Publish a real COUNT(DISTINCT country)."
    )
