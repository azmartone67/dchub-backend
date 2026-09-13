"""The two defects that made /api/v1/site-report take 14-18s, and the cap that lied.

★ 2026-08-25. The Site Analysis (`generate_site_analysis`, the Land & Power
"Generate Site Analysis" button) rendered in 14.2s cold, which is what pushed it
past the Cloudflare edge budget and produced the founding-member 503 (FE#1248
bought headroom; it did not make anything fast). The handoff blamed a cold
process-level cache. Profiling said otherwise — the cost was one query and one
timeout that was never a timeout.

1. THE QUERY.  site_planner.find_nearest_transmission matched a substation name
   against line endpoints with a LEADING wildcard:

       WHERE LOWER(sub_1) LIKE LOWER('%OSM-917634654%') OR LOWER(sub_2) LIKE ...

   A leading wildcard can never become an index qual, so the planner walked
   idx_transmission_voltage over the entire table. Measured EXPLAIN on prod:

       Index Scan Backward using idx_transmission_voltage
         Rows Removed by Filter: 2821162
         Buffers: shared hit=67423 read=141133
         Execution Time: 2292.723 ms

   and, cold, "canceling statement due to statement timeout". The MISS pays the
   whole scan — and a miss is the common case, because most `substations.name`
   values are import placeholders (OSM-917634654, RISER167166) that match no
   endpoint. Anchoring the pattern lets Postgres rewrite it into a real index
   range (BitmapOr over idx_transmission_sub1/sub_2): 2.29s -> 26ms.

   ★ 2026-09-13: the lookup moved to the maintained transmission_lines (EIA,
   ~94.6K rows, endpoints from_sub/to_sub). discovered_transmission_lines was a
   frozen March 2026 crawl with no writer. A leading wildcard is a full scan on
   any table, so S1-S3 follow the new table with the same intent — and S2 also
   fails if a SQL literal in site_planner.py still reads the crawl.

   ★ 2026-09-13, step 2 anchored: the lookup no longer matches a prefix at all.
   A first word matched 797 EIA lines for WEST and served lines hundreds of miles
   from the substation, so step 2 now matches endpoint names EXACTLY —
   `from_sub = ANY(%s) OR to_sub = ANY(%s)` over the names of the substations near
   the site, uppercased in Python. The two shapes S1 exists to stop are unchanged:
   a leading wildcard, and a function around the column (UPPER() now as well as
   LOWER(), since the names are uppercased on the other side). S1-S3 read CODE —
   SQL literals from the AST, comments blanked — so a comment quoting a query can
   neither satisfy S3 nor hide from S1.

2. THE CAP THAT NEVER CAPPED.  _call_with_timeout promised "a hard wall-clock
   cap" so a slow probe could not blow the report's budget. It was written as

       with _cf.ThreadPoolExecutor(max_workers=1) as ex:
           return ex.submit(fn, ...).result(timeout=timeout)

   ThreadPoolExecutor.__exit__ calls shutdown(wait=True), which JOINS the
   worker. So `.result(timeout=6)` raised at 6s and then __exit__ blocked until
   the work finished anyway. Measured on the unpatched helper: a 6s cap on a 10s
   call returned None after 10.01s. Every timeout in that file was decorative,
   including the 7-way section pool's _grab(timeout=18) — which is why a report
   could run 18s while no number in the source said anything larger than 6.

Why these guards are source- and behaviour-level rather than a live probe: DB
tests skip in CI, and both defects presented as a 200 with a plausible body —
the slow path returns the same report, just late. Same lesson as
[[test_substations_columns]]: a 200 is not an answer.

THE CONTRACT
────────────
  T1. _call_with_timeout returns at its deadline, not when the work finishes.
  T2. Anti-vacuity control for T1: the victim function really is slow, and the
      unpatched `with`-form really does block (so T1 can fail).
  T3. _call_with_timeout still returns the value on the happy path.
  S1. No function that reads transmission_lines — nor one it calls or that calls
      it, where the query's parameters get built — builds a leading-wildcard
      pattern, and no SQL literal reading it wraps from_sub/to_sub in LOWER() or
      UPPER().
  S2. Anti-vacuity control for S1: the scan finds that query and its endpoint
      predicate, S1's patterns match the shapes they exist to catch, comments are
      invisible to it, and no SQL literal still reads the retired
      discovered_transmission_lines.
  S3. Positive control: the exact `from_sub = ANY(%s) OR to_sub = ANY(%s)` form is
      the one in use, with the names uppercased in Python.
  P1. _build_survey_data's section pool is not a `with` block — otherwise its
      per-section _grab(timeout=18) cannot bound the request either.

EXPECTED PASS/FAIL — MEASURED, not predicted.
─────────────────────────────────────────────
  patched   : 7 passed, exit 0
  unpatched : 4 failed, 3 passed, exit 1 — T1 (returned after ~1.0s for a 0.25s
              cap), S1 (LOWER(sub_N) LIKE), S3 (anchored form absent), P1 (the
              `with` pool). T2/S2 are controls and pass in BOTH trees by design;
              T3 is the happy path and must survive the fix. Run against
              origin/main 1117752c, not a hand-edited copy.
"""

import ast
import concurrent.futures as _cf
import io
import pathlib
import re
import textwrap
import time
import tokenize

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
SITE_PLANNER = REPO / "site_planner.py"
SITE_REPORT = REPO / "routes" / "site_report.py"


def _load(fn):
    """Import _call_with_timeout without dragging in the Flask blueprint."""
    import importlib
    mod = importlib.import_module("routes.site_report")
    return getattr(mod, fn)


# ── T1/T2/T3 — the cap must actually cap ─────────────────────────────────────

def test_t1_call_with_timeout_returns_at_its_deadline():
    """T1: a 0.25s cap on a 1.0s call must return in well under 1.0s."""
    call = _load("_call_with_timeout")

    def slow():
        time.sleep(1.0)
        return "finished anyway"

    t0 = time.time()
    result = call(slow, 0.25)
    elapsed = time.time() - t0

    assert result is None, f"expected None on timeout, got {result!r}"
    assert elapsed < 0.7, (
        f"_call_with_timeout(timeout=0.25) took {elapsed:.2f}s on a 1.0s call — "
        "the cap is not enforced. ThreadPoolExecutor.__exit__ does "
        "shutdown(wait=True), which joins the worker; build the executor by "
        "hand and shutdown(wait=False) instead."
    )


def test_t2_control_the_victim_is_slow_and_the_with_form_blocks():
    """T2: anti-vacuity. Proves T1 is capable of failing.

    Without this, T1 would pass trivially if `slow()` were not actually slow.
    """
    def slow():
        time.sleep(1.0)
        return "finished anyway"

    # (a) the victim really does take ~1s
    t0 = time.time()
    slow()
    assert time.time() - t0 >= 0.9, "control invalid: slow() is not slow"

    # (b) the ORIGINAL `with`-form really does block past its cap
    t0 = time.time()
    try:
        with _cf.ThreadPoolExecutor(max_workers=1) as ex:
            ex.submit(slow).result(timeout=0.25)
    except Exception:
        pass
    blocked = time.time() - t0
    assert blocked >= 0.9, (
        f"control invalid: the `with` form returned in {blocked:.2f}s, so T1 "
        "would pass even unpatched"
    )


def test_t3_call_with_timeout_still_returns_the_value():
    """T3: the fix must not break the happy path."""
    call = _load("_call_with_timeout")
    assert call(lambda a, b=0: a + b, 5, 2, b=3) == 5


# ── S1/S2/S3 — the transmission query must be index-usable ───────────────────

# A LIKE pattern with a leading wildcard, in the three shapes code builds one:
# a literal ('%OSM-917634654%'), an f-string (f"%{term}%"), a concatenation
# ('%' + term). A bare %s placeholder is none of them.
_LEADING_WILDCARD = re.compile(r"""['"]%(?:\{|['"]\s*\+|[A-Za-z_])""")

# A function around an endpoint column: no index on from_sub/to_sub can serve it.
_COLUMN_SIDE_FN = re.compile(r"\b(?:LOWER|UPPER)\s*\(\s*(?:from_sub|to_sub)\b", re.I)

# The maintained table as a whole word, so the retired crawl's name
# (discovered_transmission_lines) can never satisfy it.
_TX_TABLE = r"(?<![A-Za-z0-9_])transmission_lines(?![A-Za-z0-9_])"
_RETIRED_TX_TABLE = "discovered_transmission_lines"


def _docstring_ids(tree):
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                ids.add(id(first.value))
    return ids


def _sql_literals(node, docstrings):
    """String constants under `node`, docstrings excluded. Comments never reach the
    AST, so prose about a query cannot stand in for the query."""
    return [n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docstrings]


def _reads_tx(literal):
    return re.search(r"(FROM|JOIN)\s+" + _TX_TABLE, literal, re.I)


def _code_without_comments(src, node):
    """The source of `node` with every comment blanked out, positions kept."""
    seg = textwrap.dedent(ast.get_source_segment(src, node, padded=True))
    lines = seg.splitlines(keepends=True)
    for tok in tokenize.generate_tokens(io.StringIO(seg).readline):
        if tok.type == tokenize.COMMENT:
            (row, col), (_, end) = tok.start, tok.end
            lines[row - 1] = lines[row - 1][:col] + " " * (end - col) + lines[row - 1][end:]
    return "".join(lines)


def _tx_scope(src):
    """Every module-level function with a SQL literal reading transmission_lines,
    plus the module-level functions it calls and those that call it — where that
    query's parameters get built. Entries: (name, node, its tx literals, code)."""
    tree = ast.parse(src)
    docs = _docstring_ids(tree)
    fns = {n.name: n for n in tree.body
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def calls(fn):
        return {c.func.id for c in ast.walk(fn)
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                and c.func.id in fns}

    readers = {name for name, fn in fns.items()
               if any(_reads_tx(s) for s in _sql_literals(fn, docs))}
    scope = set(readers)
    for name in readers:
        scope |= calls(fns[name])
    scope |= {name for name, fn in fns.items() if calls(fn) & readers}
    return [(name, fns[name],
             [s for s in _sql_literals(fns[name], docs) if _reads_tx(s)],
             _code_without_comments(src, fns[name]))
            for name in sorted(scope)]


def test_s1_no_leading_wildcard_or_column_side_function_on_transmission_endpoints():
    """S1: the endpoint match stays index-usable — no '%term' pattern, and no
    LOWER()/UPPER() around from_sub/to_sub."""
    src = SITE_PLANNER.read_text()
    offenders = []
    for name, _, literals, code in _tx_scope(src):
        for literal in literals:
            m = _COLUMN_SIDE_FN.search(literal)
            if m:
                offenders.append(f"{name}: `{m.group(0)}...` — a function on the column "
                                 "defeats any index on from_sub/to_sub")
        m = _LEADING_WILDCARD.search(code)
        if m:
            offenders.append(f"{name}: leading-wildcard pattern {m.group(0)!r}")
    assert not offenders, (
        "un-indexable endpoint match against transmission_lines: "
        + "; ".join(offenders)
        + ". Match the names exactly (from_sub = ANY(%s)), uppercased in Python. "
          "Do NOT add COLLATE \"C\" — that defeats the index."
    )


def test_s2_control_the_scan_finds_the_transmission_query():
    """S2: anti-vacuity. If this fails, S1 proved nothing."""
    src = SITE_PLANNER.read_text()
    scope = _tx_scope(src)
    literals = [lit for _, _, lits, _ in scope for lit in lits]
    assert literals, "scan found no SQL literal reading transmission_lines"
    assert any(re.search(r"\bWHERE\b.*\bfrom_sub\b.*\bto_sub\b", lit, re.I | re.S)
               for lit in literals), (
        "scan found the table but no predicate on from_sub/to_sub — S1 would pass vacuously"
    )
    names = {name for name, *_ in scope}
    # The entry point runs step 1 and hands its rows to the endpoint query. Since
    # 2026-09-13 that is find_nearest_transmission_measured; find_nearest_transmission
    # only unwraps its answer and builds nothing.
    assert "find_nearest_transmission_measured" in names, (
        f"S1's scope {sorted(names)} misses the lookup's entry point, where a pattern "
        "could be built and passed in"
    )
    # S1's patterns match the shapes they exist to catch, and not the placeholder.
    for shape in ("LOWER(from_sub) LIKE LOWER(%s)", "UPPER( to_sub ) = ANY(%s)"):
        assert _COLUMN_SIDE_FN.search(shape), shape
    for shape in ('f"%{search_term}%"', "'%' + term", '"%"+term', "'%OSM-917634654%'"):
        assert _LEADING_WILDCARD.search(shape), shape
    assert not _LEADING_WILDCARD.search("WHERE from_sub LIKE %s"), "a bare %s is not a wildcard"
    # Comments are invisible to the scan: the same shapes in one cannot trip S1.
    commented = "def f():\n    # f\"%{term}%\" and UPPER(from_sub)\n    return 1\n"
    assert not _LEADING_WILDCARD.search(
        _code_without_comments(commented, ast.parse(commented).body[0]))
    tree = ast.parse(src)
    stale = [lit for lit in _sql_literals(tree, _docstring_ids(tree)) if _RETIRED_TX_TABLE in lit]
    assert not stale, (
        "a SQL literal in site_planner.py still reads discovered_transmission_lines, "
        "the frozen March 2026 crawl retired 2026-09-13 — read transmission_lines"
    )


def test_s3_positive_control_the_exact_anchored_form_is_in_use():
    """S3: the replacement is present, not merely the offender absent."""
    src = SITE_PLANNER.read_text()
    readers = [(name, fn, lits) for name, fn, lits, _ in _tx_scope(src) if lits]
    assert any(re.search(r"from_sub = ANY\(%s\) OR to_sub = ANY\(%s\)", lit)
               for _, _, lits in readers for lit in lits), (
        "expected the exact `from_sub = ANY(%s) OR to_sub = ANY(%s)` form in a SQL literal"
    )
    assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == "upper"
               for _, fn, _ in readers for n in ast.walk(fn)), (
        "expected the names uppercased in Python (name.upper()) in the function that "
        "runs the endpoint query: endpoints are stored uppercase, and a function on "
        "the column defeats its index"
    )


# ── P1 — the section pool must not join on exit either ───────────────────────

def test_p1_section_pool_does_not_join_on_exit():
    """P1: a `with` around the 7-way pool makes _grab(timeout=18) decorative."""
    src = SITE_REPORT.read_text()
    assert "with _cf.ThreadPoolExecutor(max_workers=7)" not in src, (
        "_build_survey_data's section pool is a `with` block, so "
        "ThreadPoolExecutor.__exit__ joins every section and the per-section "
        "_grab(timeout=18) cannot bound the request"
    )
    assert "ex.shutdown(wait=False" in src, (
        "expected the section pool to be shut down without waiting"
    )
