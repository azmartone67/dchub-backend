"""capacity_pipeline quarantine-guard drift fence — 2026-07-31.

main.py carried this, stamped by the 2026-07-27 pipeline-GW audit:

    ★★2026-07-27 pipeline-GW audit — every capacity_pipeline read now
    excludes quarantined rows [...] Never drop the data_flag guard from
    a published figure.

"Every read" was ONE read — the `curated_pipeline_*` block the comment sat
in. Fourteen other live served reads published the unfiltered number.
Measured against the Neon read replica 2026-07-31:

    SUM(capacity_mw) unfiltered                       = 2,680.6 GW
    SUM(capacity_mw) WHERE COALESCE(data_flag,'')=''  =   586.6 GW   (4.6x)
    rows                                        1,973 →     1,248

WHY THE 07-27 GUARD DIDN'T HOLD
--------------------------------
It was a function-local variable (`_CP_OK = "..."` inside one function in
main.py). Nothing could import it, so nothing could be checked against it,
so nothing noticed the other fourteen. The predicate now lives in
util/capacity_pipeline.CP_OK and this file is what makes "every read" true
by construction instead of by assertion.

WHAT THIS FENCE CHECKS
----------------------
1. Every SQL literal in a SERVED module that says `FROM capacity_pipeline`
   also carries the guard — either the literal text `data_flag`, or an
   f-string interpolation of CP_OK / cp_ok(...).
2. A CENSUS, not a presence check. Per the #2062 lesson: a fence that only
   asserts "the guard appears somewhere in this file" goes green the moment
   ONE read is guarded, which is exactly the state 07-27 left us in. This
   counts guarded vs unguarded sites per file and fails on any unguarded
   one.
3. Anti-vacuous floor. If the sweep finds fewer than _MIN_GUARDED_SITES
   guarded reads it fails even with zero violations — otherwise a refactor
   that moves SQL into a string builder (invisible to an AST literal scan)
   would silently turn this fence into a no-op that reports success.
4. CP_OK contains no literal `%`. Several callers interpolate it into a
   query that ALSO passes a params tuple; a `%` would make psycopg2 attempt
   substitution and 500 the route.
5. The two dead-column reads this sweep removed do not come back:
   capacity_pipeline has no `power_mw` and no `mw` column.

   Only those two are banned outright. The table also lacks `iso`, `state`,
   `city`, `first_seen`, `company`, `investment` and `expected_completion`,
   and reads using them survive on purpose in routes/iso_snapshot.py,
   routes/changes_feed.py, routes/persona_briefs.py and
   dchub_mcp_server.py::get_pipeline — each is documented in place as dead,
   each is an UNDER-claim (returns None/[]/falls back to REST rather than an
   inflated number), and each needs a data-modelling decision rather than a
   column swap. Banning those names here would force a rushed guess; the
   guard is pre-applied to each instead so the eventual repair inherits it.

DELIBERATELY NOT CHECKED
------------------------
Root-level maintenance/agent scripts (capacity_cleanup.py,
global_intelligence_agent.py, ai_orchestrator.py, db_audit.py, ...) read
the whole table on purpose — quarantined rows are KEPT, and a script
auditing extraction quality must see them. Only served surfaces are swept.
"""

import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ── what counts as a served surface ──────────────────────────────────
#
# routes/ is the blueprint tree; main.py registers it and carries its own
# endpoints; site_planner.py registers routes via
# register_site_planner_routes (main.py ~7320).
# ★ dchub_mcp_server.py is served. It is NOT reachable by grepping main.py's
# imports — start_web.sh launches `bash start_mcp.sh &` on the web role, which
# runs it as a sidecar process on port 8888, and main.py proxies /sse to
# 127.0.0.1:8888. It was missed on the first pass of the 07-31 sweep for
# exactly that reason: "does main.py import it?" is the wrong reachability
# test in this repo. When adding a served surface here, check the launcher
# scripts too, not just the import graph.
_SERVED_DIRS = ("routes",)
_SERVED_FILES = ("main.py", "site_planner.py", "check_stats.py",
                 "dchub_mcp_server.py")

# Sites that are legitimately unguarded, each with the reason it is safe.
# Keyed by relative path -> set of substrings identifying the query.
_ALLOW = {
    # PR #2064 gated this corpus at the RAG registry (`CORPORA[...]["where"]`
    # carries `coalesce(t.data_flag,'') = ''`), which is the gate retrieval
    # actually honours. THIS entry is the downstream hydrate-by-id lookup:
    # it only enriches ids that retrieval already returned. Double-gating it
    # would silently drop citation fields for any row that slipped through,
    # masking an incomplete _sweep_orphans drain instead of surfacing it.
    # brain_rag.py owns its own guard; do not add a second one here.
    "routes/brain_rag.py": {"id::text = ANY(%s)"},
}

# Minimum number of GUARDED capacity_pipeline reads the sweep must still be
# able to see. Set below the 2026-07-31 count (33, after dchub_mcp_server.py
# joined the served set) with headroom for consolidation; raise it if that
# number grows, never lower it to make a refactor pass.
_MIN_GUARDED_SITES = 26

_GUARD_TOKENS = ("data_flag", "CP_OK", "cp_ok")
_TABLE_RE = re.compile(r"\bfrom\s+capacity_pipeline\b", re.I)


def _iter_served_files():
    for rel in _SERVED_FILES:
        p = os.path.join(ROOT, rel)
        if os.path.exists(p):
            yield rel, p
    for d in _SERVED_DIRS:
        base = os.path.join(ROOT, d)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [x for x in dirnames
                           if not x.startswith('.') and x != '__pycache__']
            for fn in sorted(filenames):
                if fn.endswith(".py"):
                    p = os.path.join(dirpath, fn)
                    yield os.path.relpath(p, ROOT), p


def _sql_units(tree):
    """Yield (lineno, reconstructed_sql) for every string-ish node.

    An f-string's FormattedValue slots are replaced by their SOURCE
    expression (via ast.unparse), so `f"... WHERE {CP_OK}"` reconstructs as
    "... WHERE CP_OK" and the guard token is visible to the scan. Adjacent
    literal concatenation (`"SELECT ... " f"FROM ... {CP_OK}"`) is folded by
    the parser into a single JoinedStr, so it reconstructs whole.

    ★ ast.walk descends INTO a JoinedStr and yields its literal Constant
    chunks as nodes in their own right. Those chunks stop at the `{`, so
    `f"... FROM capacity_pipeline WHERE {CP_OK}"` would surface a bare
    "... FROM capacity_pipeline WHERE " with the guard sliced off and every
    correctly-guarded read would be reported as a violation. Collect the
    JoinedStr-owned Constants first and skip them at the top level.
    """
    inner = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for part in node.values:
                if isinstance(part, ast.Constant):
                    inner.add(id(part))

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in inner:
                continue
            yield node.lineno, node.value
        elif isinstance(node, ast.JoinedStr):
            parts = []
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    parts.append(part.value)
                elif isinstance(part, ast.FormattedValue):
                    try:
                        parts.append(ast.unparse(part.value))
                    except Exception:
                        parts.append(" ")
            yield node.lineno, "".join(parts)


def _parse(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return ast.parse(f.read())


def test_every_served_capacity_pipeline_read_carries_the_guard():
    """Census: no served `FROM capacity_pipeline` may lack the guard."""
    violations = []
    guarded = []
    for rel, path in _iter_served_files():
        try:
            tree = _parse(path)
        except SyntaxError:
            continue  # parseability is syntax-check's job
        allowed = _ALLOW.get(rel, set())
        for lineno, sql in _sql_units(tree):
            if not _TABLE_RE.search(sql):
                continue
            if any(a in sql for a in allowed):
                continue
            if any(tok in sql for tok in _GUARD_TOKENS):
                guarded.append(f"{rel}:{lineno}")
            else:
                violations.append(
                    f"{rel}:{lineno}: reads capacity_pipeline with no "
                    f"data_flag guard -> {' '.join(sql.split())[:110]}")

    assert not violations, (
        "Unguarded capacity_pipeline read on a served surface. The table "
        "carries 725 quarantined rows (2,680.6 GW vs 586.6 GW publishable); "
        "add `AND {CP_OK}` from util.capacity_pipeline. See this file's "
        "docstring.\n" + "\n".join(sorted(set(violations))))

    # Anti-vacuous: a green result must mean the sweep actually saw the reads.
    assert len(guarded) >= _MIN_GUARDED_SITES, (
        f"Fence found only {len(guarded)} guarded capacity_pipeline reads, "
        f"expected >= {_MIN_GUARDED_SITES}. Zero violations is therefore not "
        f"evidence of coverage — the SQL has probably moved somewhere this "
        f"AST literal scan cannot see. Found: {sorted(guarded)}")


def test_agent_index_coverage_summary_guards_the_pipeline_count():
    """The one served read the literal scan structurally cannot see.

    routes/agent_index.py::_coverage_summary counts rows with a generic
    f"SELECT COUNT(*) FROM {table}" helper, so no literal ever contains the
    string "capacity_pipeline" and the sweep above is blind to it. Assert
    directly that the loop pairs that table with a guard.
    """
    path = os.path.join(ROOT, "routes", "agent_index.py")
    tree = _parse(path)

    func = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef)
                 and n.name == "_coverage_summary"), None)
    assert func is not None, "routes/agent_index.py::_coverage_summary is gone"

    src = ast.unparse(func)
    assert "capacity_pipeline" in src, "expected the pipeline count to remain"

    # Find the tuple whose first element is the table name, and require a
    # guard token somewhere in that same tuple.
    rows = [n for n in ast.walk(func) if isinstance(n, ast.Tuple)]
    hit = None
    for t in rows:
        if t.elts and isinstance(t.elts[0], ast.Constant) \
                and t.elts[0].value == "capacity_pipeline":
            hit = ast.unparse(t)
            break
    assert hit is not None, (
        "_coverage_summary no longer pairs 'capacity_pipeline' with its "
        "per-table WHERE in a tuple — re-check the guard by hand and update "
        "this test")
    assert any(tok in hit for tok in _GUARD_TOKENS), (
        f"_coverage_summary counts capacity_pipeline unguarded: {hit}")


def test_cp_ok_has_no_literal_percent():
    """A `%` in the predicate would break every caller that passes params."""
    from util.capacity_pipeline import CP_OK, cp_ok
    assert "%" not in CP_OK, (
        f"CP_OK must stay %-free (psycopg2 would try to substitute it in "
        f"queries that also pass a params tuple): {CP_OK!r}")
    assert "%" not in cp_ok("cp")
    assert cp_ok() == CP_OK
    assert cp_ok("cp") == "COALESCE(cp.data_flag,'') = ''"


def test_no_phantom_power_mw_or_mw_column_on_capacity_pipeline():
    """capacity_pipeline has capacity_mw. It has never had power_mw or mw.

    routes/power_totals.py summed `power_mw` inside `except Exception: pass`
    and could only ever contribute 0; check_stats.py printed an error row on
    every run. Verified against information_schema on 2026-07-31: 0 matching
    columns for either name.
    """
    # `power_mw` anywhere is wrong on this table. Bare `mw` is only flagged in
    # a COLUMN-REFERENCE position — inside an aggregate, or as the subject of
    # a NULL/comparison test. `mw` is also a perfectly good output alias
    # (monthly_trend does `SUM(capacity_mw) AS mw ... ORDER BY mw DESC`), and
    # flagging every occurrence would make this fence cry wolf until someone
    # deleted it.
    bad = [
        re.compile(r"\bpower_mw\b", re.I),
        re.compile(r"\b(?:sum|avg|max|min|count)\s*\(\s*mw\s*\)", re.I),
        re.compile(r"\bmw\s+(?:is\s+(?:not\s+)?null|[<>=!])", re.I),
    ]
    violations = []
    for rel, path in _iter_served_files():
        try:
            tree = _parse(path)
        except SyntaxError:
            continue
        for lineno, sql in _sql_units(tree):
            if not _TABLE_RE.search(sql):
                continue
            # Strip the legitimate column first so `capacity_mw` cannot match
            # the `power_mw` pattern by suffix.
            stripped = re.sub(r"\bcapacity_mw\b", "", sql, flags=re.I)
            if any(p.search(stripped) for p in bad):
                violations.append(f"{rel}:{lineno}: "
                                  f"{' '.join(sql.split())[:110]}")
    assert not violations, (
        "capacity_pipeline query references a `power_mw`/`mw` column that "
        "does not exist — it will raise UndefinedColumn and, in a try/except, "
        "silently contribute 0. The column is `capacity_mw`.\n"
        + "\n".join(sorted(set(violations))))
