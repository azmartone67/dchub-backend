"""`deals` quarantine-guard drift fence — 2026-08-01.

util/capacity_pipeline.py exists because a predicate that lives as a
function-local variable cannot be imported, therefore cannot be checked,
therefore drifts. Its own docstring names `deals` as the motivating
example and lists three hand-copies of the deals predicate. PR #2071 then
added two more, because consolidating was out of that PR's scope. Counted
2026-08-01 there were SEVEN files carrying it, in two spellings, two of
them function-local:

    routes/graph_spine_master_shell.py   _LIVE_DEALS  + 3 inline
    routes/infra_growth.py               _live        ← function-LOCAL
    routes/deals_routes.py               3 inline
    routes/agent_index.py                DEALS_OK     ← function-LOCAL (#2071)
    canonical_stats.py                   1 inline
    routes/brain_rag.py                  1 inline (aliased)
    routes/agreement_master_shell.py     1 inline (inverted)

All seven now import from util/deals.py. This file is what keeps that
true, and — more importantly — what keeps the count of UNGUARDED reads
from growing back.

WHAT THE GUARD IS WORTH (measured 2026-08-01)
---------------------------------------------
    deals, raw                                4,711 rows
    deals, WHERE DEALS_OK                     1,843 rows      (2.6x)

    quarantine_duplicate        2,823
    NULL                        1,836
    quarantine_unit_garbage        34
    quarantine_seed_placeholder    11
    cumulative_capex                7   ← publishable, and why this
                                          table needs the PREFIX test

Published canon is "1,400+". The unguarded 4,711 is the stale "4,000+".

WHAT THIS FENCE CHECKS
----------------------
1. A CENSUS, not a presence check. Per the #2062 lesson, a fence that
   asserts "the guard appears somewhere in this file" goes green the
   moment ONE read is guarded — which is precisely the state the 07-27
   capacity_pipeline audit left us in, and precisely the state `deals`
   was in this morning (7 guarded reads, 116 unguarded). This counts
   guarded vs unguarded `FROM deals` per file and compares each file
   against a recorded number.

2. The comparison is EXACT (==), not <=. That is deliberate and it is
   what makes the fence anti-vacuous per-file:
     * count goes UP   -> a new unguarded read landed. Add the guard.
     * count goes DOWN -> either you guarded one (good: decrement the
       baseline in the same PR) or the SQL moved somewhere this AST
       literal scan cannot see (bad: the fence just went partially
       blind, and a <= comparison would have reported success).
   A refactor that hides every query behind a string builder therefore
   FAILS here instead of silently turning the fence into a no-op.

3. An anti-vacuous FLOOR on the guarded side as well
   (_MIN_GUARDED_SITES). Both counters collapsing at once — the shape a
   whole-file rewrite or a broken sweep produces — trips this even if
   the per-file baselines somehow balanced out.

4. DEALS_OK contains no literal `%`. Several callers concatenate or
   interpolate it into a query that ALSO passes a params tuple
   (dchub_mcp_server.list_transactions, main.py's /api/transactions,
   routes/brain_rag.py's corpus `where`); a literal `%` makes psycopg2
   attempt substitution and 500s the route. This is why the predicate is
   LEFT(data_flag,11) and not LIKE 'quarantine_%'.

5. The PREFIX form survives. `deals` must NOT be "harmonised" onto
   capacity_pipeline's strict `COALESCE(data_flag,'') = ''`: seven live
   rows carry `cumulative_capex`, a legitimate non-quarantine flag, and
   the strict form would silently drop them from every published figure.
   The two modules disagreeing is correct.

6. The predicate is equivalent to BOTH spellings it replaced, over the
   live flag vocabulary. Modelled in Python — this repo's tests have no
   database — so it checks the LOGIC of the rewrite, not Postgres.

7. The two served reads the literal scan structurally CANNOT see are
   asserted directly (see test_structurally_invisible_guards).

DELIBERATELY NOT CHECKED
------------------------
Root-level maintenance scripts (cleanup_deals.py, fix_duplicates_direct.py,
seed_deals_v3.py, deal_scraper.py, ...) read the whole table on purpose —
quarantined rows are KEPT, and a script auditing extraction quality must
see them. Only served surfaces are swept. `_SERVED_FILES` matches
tests/test_capacity_pipeline_guard.py's set plus the root modules main.py
imports that actually read `deals` (canonical_stats, public_endpoints,
agent_hub, mcp_gateway). ★ As that fence's docstring warns, "does main.py
import it?" is the wrong reachability test here — dchub_mcp_server.py is
launched as a sidecar by start_web.sh, not imported. Check the launcher
scripts too when adding a surface.

An OBSERVATION, not a claim this fence makes: several baselined queries
reference columns `deals` does not appear to have (`announced_at`,
`title`, `value_usd`, `deal_hash`, `asset_name`). If so they raise and
are swallowed, which makes them dead rather than wrong. They are left in
the baseline as unguarded because that is what they are; diagnosing them
needs a live schema check, not a guard.
"""

import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ── what counts as a served surface ──────────────────────────────────
_SERVED_DIRS = ("routes",)
_SERVED_FILES = ("main.py", "site_planner.py", "check_stats.py",
                 "dchub_mcp_server.py", "canonical_stats.py",
                 "public_endpoints.py", "agent_hub.py", "mcp_gateway.py")

# Identifiers that mean "this query is guarded". Only the canonical names
# from util/deals.py, plus the raw column for queries that reference
# data_flag directly (a flag breakdown, say). Local aliases are NOT listed
# on purpose: an alias hides the guard from this census, so the call sites
# spell DEALS_OK / deals_ok() out. main.py imports it as `_DEALS_OK`, which
# the DEALS_OK substring still matches.
_GUARD_TOKENS = ("data_flag", "DEALS_OK", "deals_ok",
                 "DEALS_QUARANTINED", "deals_quarantined")

# `FROM deals`, but only inside something that is actually a query. Two
# module docstrings (routes/operator_brief.py::_section_capital,
# routes/site_valuation_engine.py) contain the English phrase "from deals"
# in prose; requiring a SELECT in the same unit drops both without an
# exception list that would rot the moment the prose is reworded.
_TABLE_RE = re.compile(r"\bfrom\s+deals\b", re.I)
_SELECT_RE = re.compile(r"\bselect\b", re.I)
# DELETE/UPDATE/INSERT are not published reads; the admin dedup console
# and the id-repair endpoints are supposed to touch quarantined rows.
_WRITE_RE = re.compile(r"^\s*(delete|update|insert)\b", re.I)


# ── reads that are unguarded ON PURPOSE, each with the reason ─────────
#
# Keyed by relative path -> set of matchers. A matcher is either
# "func:<name>" (every deals read inside that function) or a substring of
# the rendered SQL. An entry here is a claim that reading quarantined rows
# is CORRECT for that call site, not that guarding it is inconvenient.
#
# ★ Function-scoped, not SQL-scoped, wherever the query is a bare
# `SELECT COUNT(*) FROM deals` — that string is indistinguishable from the
# published counts this fence exists to catch, so allow-listing the TEXT
# would punch a file-wide hole. And because a function-scoped exemption
# could still hide a NEW read added inside it, the allow-list is itself
# censused: see _ALLOW_COUNT.
_ALLOW = {
    # Admin dedup / QA console. Reports rows-before and rows-after around
    # its own DELETEs and partitions candidates for removal; guarding it
    # would hide exactly the rows it exists to act on. Whole file.
    "routes/admin_ai_deals.py": {"*"},

    # Row-count integrity sweep: compares COUNT(*) and COUNT(DISTINCT
    # deal_hash) against sentinel_row_baselines to detect unexpected
    # deletion. A guarded count would drift against the stored baseline
    # every time the quarantine grows and alert on its own filter.
    "routes/surveillance_sweep.py": {"*"},

    "dchub_mcp_server.py": {
        # A backup inventory + its freshness probe. Backups contain every
        # row, quarantined included, so a guarded count would understate
        # what is actually stored.
        "func:get_backup_status",
        # list_transactions builds `FROM deals {where}` and seeds `where`
        # from a conditions list this literal scan cannot follow. Asserted
        # structurally instead — see test_structurally_invisible_guards.
        "func:list_transactions",
    },

    "main.py": {
        # `stats['deals_rows']` — documented in place as the RAW row count;
        # `total_deals` beside it is repointed to the deduped canon, so the
        # raw number is the point of the field.
        "func:get_stats",
        # Admin dedup/repair console: finds duplicate groups, inspects
        # candidates and reports post-DELETE totals.
        "func:admin_deals_cleanup",
        "func:admin_update_deal",
    },

    # Same documented pattern as main.py's: `deals_rows` is the raw count
    # published NEXT TO the canonical deduped one, which comes from
    # canonical_stats. Removing the raw number is a product decision.
    "routes/facilities_by_dims.py": {"func:stats_canonical"},

    "routes/jobs_routes.py": {
        # Database-growth time series. Its sibling row deliberately counts
        # the legacy `facilities` table for the same reason: the subject is
        # the database itself, and changing the filter breaks the series.
        "func:job_db_health_snapshot",
        # Post-ingest verification: did the autopilot's writes land? Must
        # count every row it just wrote, quarantined or not.
        "func:job_autopilot",
    },

    # AUTO-<yyyymmdd>- id minting history: measures which ids were EVER
    # minted, deliberately across the whole table, to prove the 07-17
    # writer fix holds. Scoping it to served rows would hide a regression
    # that only shows up in quarantined rows. SQL-scoped, because the same
    # function carries three reads that ARE guarded.
    "routes/graph_spine_master_shell.py": {"substring(id from 6 for 8)"},

    # Hydrate-by-id for rows retrieval already returned. brain_rag's own
    # CORPORA[...]["where"] carries deals_ok("t") and IS the gate (#2064);
    # double-gating here would silently drop citation fields for a row
    # that slipped through, masking an incomplete _sweep_orphans drain
    # instead of surfacing it. Exactly the carve-out
    # tests/test_capacity_pipeline_guard.py makes for the same file.
    "routes/brain_rag.py": {"id::text = ANY(%s)"},
}

# How many reads each _ALLOW entry may cover. Censused for the same reason
# the unguarded reads are: a function-scoped exemption that is not counted
# is a place a new unguarded read can be added invisibly. Exact match —
# add a read to an allow-listed function and this trips, which forces the
# "is this one also legitimately raw?" question to be answered explicitly.
_ALLOW_COUNT = {
    "routes/admin_ai_deals.py": 8,
    "routes/surveillance_sweep.py": 5,
    "dchub_mcp_server.py": 3,
    "main.py": 7,
    "routes/facilities_by_dims.py": 1,
    "routes/jobs_routes.py": 2,
    "routes/graph_spine_master_shell.py": 1,
    "routes/brain_rag.py": 1,
}

# ── the census baseline ──────────────────────────────────────────────
#
# Reads that are NOT guarded and NOT allow-listed: per-market / per-operator
# / per-quarter brief queries, already narrowed by buyer, market or date, so
# they under-report rather than publish a headline count. Guarding them is
# correct and is the enumerated follow-on; each needs its own check that the
# surface still renders, which is a different PR from this one.
#
# ★ This dict is a RATCHET. Guard a read -> decrement its file here in the
# same commit. Never increment an entry to make a new read pass.
_BASELINE = {
    "routes/agent_winback_digest.py": 2,
    "routes/agentic_master_shell.py": 2,
    "routes/analyst_note.py": 1,
    "routes/autopilot_routes.py": 1,
    "routes/brain_inspector.py": 1,
    "routes/comprehensive_report.py": 4,
    "routes/deal_autopsy.py": 1,
    "routes/deals_routes.py": 1,
    "routes/extractor_brain.py": 1,
    "routes/hyperscaler_brief.py": 5,
    "routes/market_brief.py": 6,
    "routes/market_deep_dive.py": 1,
    "routes/marketing_engine.py": 1,
    "routes/monthly_trend.py": 4,
    "routes/operator_brief.py": 6,
    "routes/operators.py": 2,
    "routes/press_outreach.py": 1,
    "routes/quarterly_report.py": 6,
    "routes/state_brief.py": 2,
    "routes/transactions_browser.py": 1,
    "routes_stubs_v3.py": 0,
}

# Floor on the GUARDED side. 2026-08-01 census: 46 guarded, 53 unguarded
# (baselined above), 28 allow-listed. Before this consolidation it was 7
# guarded against 116 unguarded. Set below 46 with headroom for the
# follow-on work; raise it as reads are guarded, never lower it to make a
# refactor pass.
#
# ★ 2026-08-01, dead-read sweep follow-on: the four LLM live-context modules
# (media_comment_engagement, media_dm_follow_up, media_spike_responder,
# sales_outreach_automator) each dropped from 1 unguarded read to 0 — their
# `deals` queries were rewritten onto DEALS_OK when the dead columns they
# named (`title`, `value_usd`, `announced_at`) were fixed. Their rows are
# removed from _BASELINE rather than zeroed, and the guarded floor rises to
# match. site_stats.py and hyperscaler_rss.py also gained guarded `deals`
# reads replacing dead `dc_transactions` ones.
_MIN_GUARDED_SITES = 42


# ── sweep ────────────────────────────────────────────────────────────

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


def _render(node):
    """Reconstruct a string-ish expression as one SQL unit.

    Handles three shapes, because `deals` guards are written in all three:

      f"... WHERE {DEALS_OK}"          JoinedStr -> FormattedValue slots are
                                       replaced by their SOURCE expression via
                                       ast.unparse, so the token is visible.
      "... WHERE " + DEALS_OK + " ..." BinOp(+) chains are folded whole. This
                                       is the shape capacity_pipeline's fence
                                       does NOT handle, and it is the majority
                                       spelling here — without it every
                                       correctly-guarded concatenated read
                                       would be reported as a violation.
      "... WHERE data_flag ..."        a plain Constant.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out = []
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                out.append(part.value)
            elif isinstance(part, ast.FormattedValue):
                try:
                    out.append(ast.unparse(part.value))
                except Exception:
                    out.append(" ")
        return "".join(out)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _render(node.left), _render(node.right)
        if left is None and right is None:
            return None
        return (left or "") + " " + (right or "")
    if isinstance(node, (ast.Name, ast.Attribute, ast.Call)):
        try:
            return ast.unparse(node)
        except Exception:
            return None
    return None


def _sql_units(tree):
    """Yield (lineno, sql) for every string-ish node, largest unit first.

    ★ ast.walk descends INTO a JoinedStr and yields its literal Constant
    chunks as nodes in their own right. Those chunks stop at the `{`, so
    f"... FROM deals WHERE {DEALS_OK}" would ALSO surface a bare
    "... FROM deals WHERE " with the guard sliced off, and every correctly
    guarded read would be reported twice — once guarded, once not. The same
    is true of a BinOp's operands. Collect the owned children first and skip
    them at the top level.
    """
    owned = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for part in node.values:
                owned.add(id(part))
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            if _render(node) is not None:
                owned.add(id(node.left))
                owned.add(id(node.right))

    for node in ast.walk(tree):
        if id(node) in owned:
            continue
        if isinstance(node, (ast.Constant, ast.JoinedStr, ast.BinOp)):
            sql = _render(node)
            if sql:
                yield getattr(node, "lineno", 0), sql


def _parse(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return ast.parse(f.read())


def _enclosing_funcs(tree):
    """-> [(name, start, end)] innermost-first, for resolving `func:` allows."""
    spans = [(n.name, n.lineno, getattr(n, "end_lineno", n.lineno))
             for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    spans.sort(key=lambda s: s[2] - s[1])
    return spans


def _func_at(spans, lineno):
    for name, start, end in spans:
        if start <= lineno <= end:
            return name
    return ""


def _census():
    """-> (guarded, unguarded_by_file, allowed_by_file) over served surfaces."""
    guarded = []
    unguarded, allowed = {}, {}
    for rel, path in _iter_served_files():
        try:
            tree = _parse(path)
        except SyntaxError:
            continue  # parseability is syntax-check's job
        allow = _ALLOW.get(rel, set())
        spans = _enclosing_funcs(tree)
        for lineno, sql in _sql_units(tree):
            if not _TABLE_RE.search(sql) or not _SELECT_RE.search(sql):
                continue
            one = " ".join(sql.split())
            if _WRITE_RE.search(one):
                continue
            func = _func_at(spans, lineno)
            exempt = ("*" in allow
                      or (func and f"func:{func}" in allow)
                      or any(not a.startswith(("func:", "*")) and a in sql
                             for a in allow))
            if exempt:
                allowed.setdefault(rel, []).append(f"{rel}:{lineno} ({func or '<module>'})")
            elif any(tok in sql for tok in _GUARD_TOKENS):
                guarded.append(f"{rel}:{lineno}")
            else:
                unguarded.setdefault(rel, []).append(f"{rel}:{lineno}: {one[:110]}")
    return guarded, unguarded, allowed


# ── tests ────────────────────────────────────────────────────────────

def test_deals_read_census_matches_the_recorded_baseline():
    """Per-file census of unguarded `FROM deals` reads, compared EXACTLY.

    Not a presence check: "some read in this file is guarded" was true of
    `deals` all morning while 116 reads had no guard at all.
    """
    guarded, unguarded, allowed = _census()

    grew, shrank = [], []
    for rel in sorted(set(_BASELINE) | set(unguarded)):
        want = _BASELINE.get(rel, 0)
        got = len(unguarded.get(rel, []))
        if got > want:
            grew.append(f"  {rel}: {want} -> {got}\n" +
                        "\n".join(f"      {v}" for v in unguarded.get(rel, [])))
        elif got < want:
            shrank.append(f"  {rel}: baseline {want}, found {got}")

    assert not grew, (
        "New UNGUARDED `FROM deals` read on a served surface. `deals` holds "
        "2,868 quarantined rows (4,711 raw vs 1,843 publishable) and published "
        "canon is \"1,400+\" — an unguarded count republishes the stale "
        "\"4,000+\". Add the guard:\n"
        "    from util.deals import DEALS_OK\n"
        "    ... f\"SELECT COUNT(*) FROM deals WHERE {DEALS_OK}\"\n"
        "If reading quarantined rows is genuinely correct here (an admin "
        "console, a backup inventory, an ingest check), add it to _ALLOW with "
        "the reason.\n" + "\n".join(grew))

    assert not shrank, (
        "Fewer unguarded `FROM deals` reads than the baseline records. If you "
        "guarded them, decrement _BASELINE in this file in the same commit — "
        "that is the ratchet working. If you did NOT, this sweep has gone "
        "partially BLIND: the SQL moved somewhere an AST literal scan cannot "
        "follow (a string builder, a helper, a table name passed as a "
        "variable) and a <=-style fence would have reported success.\n"
        + "\n".join(shrank))

    assert len(guarded) >= _MIN_GUARDED_SITES, (
        f"Fence found only {len(guarded)} guarded `deals` reads, expected "
        f">= {_MIN_GUARDED_SITES}. A clean census is therefore not evidence "
        f"of coverage — both counters collapsing at once is what a whole-file "
        f"rewrite or a broken sweep looks like. Found: {sorted(guarded)}")


def test_the_allow_list_is_itself_censused():
    """Nothing may hide inside an exemption.

    Several _ALLOW entries are function-scoped ("every deals read inside
    get_backup_status is a backup inventory"). That is the right granularity
    — a bare `SELECT COUNT(*) FROM deals` is textually identical to the
    published counts this fence catches — but an uncounted function-wide
    exemption is a place to add a new unguarded read invisibly. So the
    exemptions are counted too.
    """
    _guarded, _unguarded, allowed = _census()

    drift = []
    for rel in sorted(set(_ALLOW_COUNT) | set(allowed)):
        want = _ALLOW_COUNT.get(rel, 0)
        got = len(allowed.get(rel, []))
        if got != want:
            drift.append(f"  {rel}: recorded {want}, found {got}\n" +
                         "\n".join(f"      {s}" for s in allowed.get(rel, [])))

    assert not drift, (
        "The set of deliberately-unguarded `deals` reads changed. If a read "
        "was ADDED inside an allow-listed function, answer the question "
        "explicitly: is reading quarantined rows correct for this one too? If "
        "yes, bump _ALLOW_COUNT and extend the reason above it. If no, guard "
        "it. If a read was REMOVED, decrement.\n" + "\n".join(drift))

    assert set(_ALLOW) == set(_ALLOW_COUNT), (
        f"Every _ALLOW entry needs an _ALLOW_COUNT and vice versa; an "
        f"uncounted exemption is an unbounded one. "
        f"_ALLOW-only: {sorted(set(_ALLOW) - set(_ALLOW_COUNT))}, "
        f"_ALLOW_COUNT-only: {sorted(set(_ALLOW_COUNT) - set(_ALLOW))}")


def test_deals_ok_has_no_literal_percent():
    """A `%` in the predicate would 500 every caller that passes params.

    dchub_mcp_server.list_transactions, main.py's /api/transactions and
    brain_rag's corpus `where` all interpolate this string into a query that
    ALSO passes a params tuple. psycopg2 would try to substitute the `%`.
    This is the whole reason the predicate is LEFT(data_flag,11) rather than
    LIKE 'quarantine_%'.
    """
    from util.deals import (DEALS_OK, DEALS_QUARANTINED, deals_ok,
                            deals_quarantined)
    for name, val in (("DEALS_OK", DEALS_OK),
                      ("deals_ok('d')", deals_ok("d")),
                      ("DEALS_QUARANTINED", DEALS_QUARANTINED),
                      ("deals_quarantined('d')", deals_quarantined("d"))):
        assert "%" not in val, f"{name} must stay %-free: {val!r}"
        assert "LIKE" not in val.upper(), f"{name} must not use LIKE: {val!r}"

    assert deals_ok() == DEALS_OK
    assert deals_quarantined() == DEALS_QUARANTINED
    assert deals_ok("d") == "COALESCE(LEFT(d.data_flag,11),'') <> 'quarantine_'"


def test_deals_keeps_the_prefix_test_not_capacity_pipelines_strict_form():
    """`deals` must not be harmonised onto CP_OK's `COALESCE(data_flag,'')=''`.

    Seven live rows carry `cumulative_capex`, a legitimate non-quarantine
    annotation. The strict form drops them from every published figure,
    silently. capacity_pipeline has no such flag, so the two modules
    disagreeing here is correct and load-bearing.
    """
    from util.capacity_pipeline import CP_OK
    from util.deals import DEALS_OK

    assert "LEFT(" in DEALS_OK.upper(), (
        f"DEALS_OK lost its prefix test: {DEALS_OK!r}. `cumulative_capex` "
        f"rows would stop being published.")
    assert "quarantine_" in DEALS_OK
    assert DEALS_OK != CP_OK, (
        "DEALS_OK collapsed onto CP_OK. capacity_pipeline fails CLOSED on an "
        "unknown flag on purpose; `deals` cannot, because it carries a "
        "legitimate non-quarantine flag.")
    # The prefix length must match the literal it is compared against.
    assert "LEFT(data_flag,11)" in DEALS_OK, (
        f"len('quarantine_') is 11; DEALS_OK truncates to something else: "
        f"{DEALS_OK!r}")


def test_predicate_is_equivalent_to_both_spellings_it_replaced():
    """The seven consolidated files used two forms. Prove they agree.

    Modelled in Python over the live flag vocabulary (2026-08-01) — this
    repo's tests have no database, so this checks the LOGIC of the rewrite,
    not Postgres. `LEFT(NULL,11)` is NULL, hence the COALESCE.
    """
    from util.deals import DEALS_OK, DEALS_QUARANTINED

    def sql_left(v, n):
        return None if v is None else v[:n]

    def coalesce(*vals):
        for v in vals:
            if v is not None:
                return v
        return None

    def new_form(flag):                      # what util/deals.py ships
        return coalesce(sql_left(flag, 11), "") != "quarantine_"

    def old_form_a(flag):                    # graph_spine / brain_rag
        return coalesce(sql_left(flag, 11), "") != "quarantine_"

    def old_form_b(flag):                    # infra_growth / deals_routes /
        return flag is None or sql_left(flag, 11) != "quarantine_"   # agent_index

    vocab = {
        "quarantine_duplicate": (2823, False),
        None: (1836, True),
        "quarantine_unit_garbage": (34, False),
        "quarantine_seed_placeholder": (11, False),
        "cumulative_capex": (7, True),
        "": (0, True),
    }
    publishable = 0
    for flag, (rows, expected) in vocab.items():
        assert new_form(flag) is expected, f"DEALS_OK misjudges {flag!r}"
        assert old_form_a(flag) == new_form(flag), f"form A differs on {flag!r}"
        assert old_form_b(flag) == new_form(flag), f"form B differs on {flag!r}"
        if expected:
            publishable += rows

    assert publishable == 1843, (
        f"The live flag breakdown no longer sums to the recorded 1,843 "
        f"publishable rows (got {publishable}). If the table changed, "
        f"re-measure and update this vocabulary AND util/deals.py's "
        f"docstring together.")
    assert sum(r for r, _ in vocab.values()) == 4711

    # The complement must be exactly that — no row may be both or neither.
    assert "<>" in DEALS_OK and "=" in DEALS_QUARANTINED
    assert DEALS_OK.replace("<>", "=") == DEALS_QUARANTINED


def test_no_hand_written_copy_of_the_predicate_survives():
    """The seven consolidated files must not re-inline it.

    This is the check that would have caught #2071 adding two more copies.
    It is scoped to the whole served tree, not just those seven — a NEW
    hand-copy in an eighth file is the same defect.
    """
    # A hand-copy is a string literal that spells the quarantine prefix out.
    # util/deals.py is the one place allowed to; comments and docstrings that
    # merely mention it are not string-literal SQL, so they are matched only
    # when they also carry the comparison.
    inline = re.compile(r"(left|LEFT)\s*\(\s*[\w.]*data_flag\s*,\s*11\s*\)")
    offenders = []
    for rel, path in _iter_served_files():
        try:
            tree = _parse(path)
        except SyntaxError:
            continue
        for lineno, sql in _sql_units(tree):
            # Only flag the LITERAL text; an f-string slot rendering as
            # `DEALS_OK` or `deals_ok('t')` is the imported form and fine.
            if inline.search(sql):
                offenders.append(f"{rel}:{lineno}: {' '.join(sql.split())[:110]}")

    assert not offenders, (
        "Hand-written copy of the `deals` quarantine predicate. This is the "
        "exact defect util/deals.py exists to end — the predicate was "
        "hand-copied into seven files, two of them as function-locals that "
        "nothing could import and therefore nothing could check. Import it:\n"
        "    from util.deals import DEALS_OK, deals_ok\n"
        + "\n".join(sorted(set(offenders))))


def test_structurally_invisible_guards():
    """The served reads the literal scan cannot see, asserted directly.

    dchub_mcp_server.list_transactions builds `FROM deals {where}` where
    `where` is joined from a `conditions` list, so no literal ever contains
    both the table and the guard. Allow-listing it without this test would
    be indistinguishable from not guarding it at all.
    """
    path = os.path.join(ROOT, "dchub_mcp_server.py")
    tree = _parse(path)

    func = next((n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name == "list_transactions"), None)
    assert func is not None, "dchub_mcp_server.py::list_transactions is gone"

    src = ast.unparse(func)
    assert "FROM deals" in src, "expected list_transactions to still read deals"
    assert "conditions = [DEALS_OK]" in src, (
        "list_transactions no longer SEEDS its conditions list with DEALS_OK, "
        "so `where` can come back empty and page AI agents through 2,823 "
        "duplicate rows. The literal sweep cannot see this — re-check it by "
        "hand and update this test.")

    # And the corpus registry gate brain_rag's own allow-list depends on.
    tree = _parse(os.path.join(ROOT, "routes", "brain_rag.py"))
    src = ast.unparse(tree)
    assert "deals_ok('t')" in src, (
        "routes/brain_rag.py's CORPORA['deals']['where'] no longer carries "
        "deals_ok('t'). That registry entry IS the retrieval gate (#2064), "
        "and _ALLOW here exempts the downstream hydrate-by-id BECAUSE of it — "
        "without it, quarantined deal chunks become retrievable with a "
        "citation stamp on the unauthenticated /api/v1/rag/search.")
