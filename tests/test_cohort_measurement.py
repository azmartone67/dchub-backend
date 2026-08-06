"""Guards on the per-cohort execute_plan report.

Copilot will read this endpoint the week they ship, when six of seven tags
legitimately have no data. Every failure mode below is one we shipped in the
last seven days, in a number somebody quoted:

  · a published statistic whose population was ~80% our own probes (#2252)
  · a "sql_filters" list that described the query instead of BEING it (#2253)
  · retention divided by the CURRENT window: 14.6% published, 8.4% true (#2267)
  · a surface that reported 0 and called it success (#2244)

These tests are PURE — no DATABASE_URL, no network — so they RUN in the
pre-merge gate rather than skipping into a silent green.

★ Text-scanning assertions run against COMMENT-STRIPPED source. Three separate
tests in this repo have passed by matching the comment that explained the very
bug they were written to catch.
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "routes" / "cohort_measurement.py"
sys.path.insert(0, str(ROOT))


def _stripped() -> str:
    """Source with docstrings and comments removed.

    ★ ORDER MATTERS, and getting it wrong is silent. Stripping comments FIRST
    rewrites any docstring containing a '#' (this module's docstrings cite
    '#2252', '#2267', ...), so the later docstring .replace() no longer matches
    and the docstring survives into the "stripped" text — leaving every text
    assertion able to pass on prose. Docstrings go first.
    """
    raw = SRC.read_text(encoding="utf-8")
    tree = ast.parse(raw)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            d = ast.get_docstring(node, clean=False)
            if d:
                raw = raw.replace(d, "")
    return re.sub(r"(?m)#.*$", "", raw)


def test_stripped_helper_actually_removes_docstrings():
    """MUST-FAIL CONTROL for _stripped() itself.

    Every text assertion in this file is only as good as this helper. The
    module docstring names defects by number; if it survived stripping, a test
    could 'prove' a property by matching the paragraph describing it.
    """
    src = _stripped()
    assert "WHY IT EXISTS" not in src, "module docstring survived stripping"
    assert "FIVE HONESTY RULES" not in src
    assert "def _cohort_filters" in src, "stripping ate the actual code"


def test_module_parses_and_names_resolve():
    """An empty or gutted module would pass every text scan below.

    The AST parse is the precondition: a file that fails to parse, or that has
    had its functions renamed away, must not be able to certify anything.
    """
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    funcs = {n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert {"_cohort_filters", "_cohort_population", "_never_used_row",
            "_measured_row", "mcp_cohorts"} <= funcs, (
        f"expected functions missing — module may have been gutted: {funcs}")
    # and the free variables the report is built from actually exist
    from routes.cohort_measurement import DECLARED_COHORTS, _SQL
    assert len(DECLARED_COHORTS) == 7, DECLARED_COHORTS
    assert _SQL.strip(), "the query is empty"


# ── #2252: self-traffic must be excluded, and IMPORTED not copied ───────────

def test_externality_predicates_are_the_imported_ones():
    """The filters must be the canonical verdict, not a second copy of it.

    A hand-maintained exclusion list is how the LIKE form and its regex twin
    drifted apart for two days (mcp_calls_deloop, 2026-07-30). Assert string
    identity with the imported predicates — if someone inlines a list here,
    these stop matching.
    """
    from mcp_calls_deloop import external_platform_predicate, real_ua_predicate
    from routes.cohort_measurement import _cohort_filters

    f = _cohort_filters(7)
    assert external_platform_predicate("i.platform") in f, (
        "platform externality filter is not the imported predicate")
    assert real_ua_predicate("i.user_agent") in f, (
        "user-agent filter is not the imported predicate")


def test_no_filter_clause_hand_lists_internal_platforms():
    """MUST-FAIL CONTROL for the test above.

    That test would still pass if someone ADDED a copied list alongside the
    imported predicates. Checked BEHAVIOURALLY, against the filter clauses the
    query actually uses, rather than by scanning the file: the module's prose
    legitimately says "dchub-*, probes, test clients" when declaring what the
    population excludes, and a blunt text scan would flag that documentation
    while missing a list inlined under a different name.
    """
    from mcp_calls_deloop import external_platform_predicate, real_ua_predicate
    from routes.cohort_measurement import _cohort_filters

    imported = {external_platform_predicate("i.platform"),
                real_ua_predicate("i.user_agent")}
    for clause in _cohort_filters(7):
        if clause in imported:
            continue  # the canonical verdict, by identity
        assert len(clause) < 120, (
            f"a long non-imported clause looks like an inlined exclusion "
            f"list: {clause[:160]!r}")
        for tag in ("dchub", "harness", "uptimerobot", "python-httpx",
                    "curl/", "value-harness", "mcp-probe"):
            assert tag not in clause.lower(), (
                f"{tag!r} is hand-listed in a filter clause — the exclusion "
                f"set must be imported from mcp_calls_deloop, never copied")


def test_population_is_filtered_at_all():
    """The #2252 defect itself: a published statistic with NO externality
    filter. Two of the six filters must be the externality verdict."""
    from routes.cohort_measurement import _cohort_filters
    f = _cohort_filters(7)
    assert any("platform" in x for x in f), f
    assert any("user_agent" in x for x in f), f
    assert "i.is_real_external" in f, f


# ── #2253: the published filters must BE the filters ───────────────────────

def test_published_sql_filters_are_the_joined_where_clause():
    """`sql_filters` must be the exact list joined into the query.

    Not a prose description that drifts from the SQL underneath. The route
    joins `" AND ".join(_cohort_filters(days))` into _SQL, and publishes
    `_cohort_filters(days)` — same call, same list.
    """
    from routes.cohort_measurement import _cohort_filters, _cohort_population

    for days in (1, 7, 30, 90):
        published = _cohort_population(days)["sql_filters"]
        assert published == _cohort_filters(days), days
        # and it is a real clause list, not sentences
        for clause in published:
            assert clause.strip(), "empty filter clause"
            assert not clause.endswith("."), f"prose, not SQL: {clause!r}"

    src = _stripped()
    assert '" AND ".join(_cohort_filters(days))' in src, (
        "the WHERE clause must be built from the SAME function that is "
        "published, or the two can drift")


def test_window_filter_tracks_the_requested_days():
    """The published window must move with `days` — a hardcoded interval
    would make the declaration a lie for every non-default window."""
    from routes.cohort_measurement import _cohort_filters
    assert "interval '14 days'" in " ".join(_cohort_filters(7))
    assert "interval '60 days'" in " ".join(_cohort_filters(30))


def test_no_bound_params_because_the_predicates_carry_literal_percent():
    """external_platform_predicate contains a literal % (LIKE).

    psycopg2 interprets % only when parameters are supplied, so the execute()
    must pass exactly one argument. Adding a bound param here is a live 500,
    and it is a trap this repo has hit before.
    """
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == "execute"]
    assert calls, "no cur.execute found — did the query move?"
    for c in calls:
        assert len(c.args) == 1, (
            "cur.execute must take NO bound params: the imported predicates "
            "carry literal % and psycopg2 would eat them")
    # and the % really is in there, so this test is guarding something real
    from mcp_calls_deloop import external_platform_predicate
    assert "%" in external_platform_predicate("i.platform")


# ── #2267: retention divides by the PRIOR cohort ───────────────────────────

def test_retention_helper_is_imported_from_the_lane_that_was_fixed():
    """Re-deriving the arithmetic is how the two lanes disagree again."""
    src = _stripped()
    assert ("from routes.agent_retention_master_shell import _retention_pct"
            in src), "retention must import the #2267 helper, not re-derive it"


def test_retention_reproduces_the_2267_numbers():
    """The exact live case that exposed the defect: 7 returning, current
    cohort 48, prior cohort 83. The board printed 14.6%; retention is 8.4%."""
    from routes.cohort_measurement import _retention_pct

    assert round(_retention_pct(7, 83), 1) == 8.4
    # MUST-FAIL CONTROL: the wrong denominator, spelled out. If the helper
    # ever divides by the current window again, the first assertion fails and
    # this one shows what it would have printed instead.
    assert round(100.0 * 7 / 48, 1) == 14.6
    assert round(_retention_pct(7, 83), 1) != round(100.0 * 7 / 48, 1)


def test_empty_prior_cohort_is_unmeasured_not_zero():
    """With nobody who could return, the rate is undefined — not 0%, which
    would read as 'we measured, and nobody came back'."""
    from routes.cohort_measurement import _measured_row, _retention_pct

    assert _retention_pct(0, 0) is None
    row = _measured_row("cohort.grid_first", {
        "calls_cur": 5, "calls_prior": 0, "agents_cur": 3,
        "agents_prior": 0, "returning_agents": 0, "days": 7}, True)
    assert row["retention_pct"] is None
    assert "UNMEASURED" in row["retention_basis"]
    assert "Not 0%" in row["retention_basis"]


def test_measured_retention_names_its_denominator_in_the_sentence():
    """#2267's own convention: the published sentence states what it divided
    by, in a form a reader can recompute. The defect was an identifier swap,
    which any guard on the intermediate variable would have followed."""
    from routes.cohort_measurement import _measured_row

    row = _measured_row("cohort.front_door", {
        "calls_cur": 40, "calls_prior": 30, "agents_cur": 12,
        "agents_prior": 83, "returning_agents": 7, "days": 7}, True)
    assert row["retention_pct"] == 8.4
    m = re.search(r"(\d+) of the prior window's (\d+) agents returned = "
                  r"(\d+\.\d)%", row["retention_basis"])
    assert m, row["retention_basis"]
    # recompute the published percentage FROM THE PUBLISHED TEXT
    assert round(100.0 * int(m.group(1)) / int(m.group(2)), 1) == float(m.group(3))


# ── unknown must not read as measured-zero ─────────────────────────────────

def test_never_used_cohort_is_null_everywhere_not_zero():
    """The row Copilot sees for six of seven tags on day one."""
    from routes.cohort_measurement import _never_used_row

    row = _never_used_row("cohort.fiber_first")
    for field in ("calls", "calls_prior_window", "agents", "prior_agents",
                  "returning_agents", "retention_pct"):
        assert row[field] is None, f"{field} is {row[field]!r}, must be None"
        assert row[field] != 0, f"{field} renders 0 — unknown is not measured-zero"
    assert row["status"] == "never_used"
    assert row["comparable"] is False


def test_every_declared_cohort_always_gets_a_row():
    """A tag that has never been used must still appear, or the table cannot
    distinguish 'nobody used it' from 'we never asked'."""
    from routes.cohort_measurement import DECLARED_COHORTS
    assert set(DECLARED_COHORTS) == {
        "cohort.front_door", "cohort.delta_first", "cohort.composite_first",
        "cohort.saved_work_first", "cohort.grid_first", "cohort.fiber_first",
        "cohort.deals_first"}, (
        "the declared set must match the contract Copilot committed to")


# ── untagged is its own bucket, and nothing vanishes ───────────────────────

def test_untagged_is_labelled_and_never_folded_into_a_cohort():
    src = _stripped()
    assert '"(untagged)"' in src or "'(untagged)'" in src, (
        "the untagged bucket must be explicitly labelled")
    assert '"untagged"' in src, "untagged must be its own top-level key"
    # the reconciliation is what proves nothing was dropped
    assert '"reconciles"' in src


def test_totals_reconcile_by_construction():
    """tagged + untagged == total. A bucket that silently vanishes is the
    defect this key exists to make visible."""
    src = _stripped()
    assert "(tagged_calls + untagged_calls) == total_calls" in src


# ── volume gating: counts beside every rate ────────────────────────────────

def test_low_volume_cohorts_are_marked_not_comparable():
    """execute_plan is ~24 calls/7d in total. A cohort with 3 calls proves
    nothing, and 100% retention on 1 agent is noise wearing a decimal point."""
    from routes.cohort_measurement import _measured_row

    thin = _measured_row("cohort.deals_first", {
        "calls_cur": 3, "calls_prior": 1, "agents_cur": 2,
        "agents_prior": 1, "returning_agents": 1, "days": 7}, True)
    assert thin["comparable"] is False
    assert "not_comparable_because" in thin
    # the rate is still published — with its counts beside it
    assert thin["retention_pct"] == 100.0
    assert thin["returning_agents"] == 1 and thin["prior_agents"] == 1

    fat = _measured_row("cohort.front_door", {
        "calls_cur": 120, "calls_prior": 90, "agents_cur": 25,
        "agents_prior": 20, "returning_agents": 5, "days": 7}, True)
    assert fat["comparable"] is True
    assert "not_comparable_because" not in fat


def test_every_row_publishes_counts_beside_its_rate():
    """A reader must be able to judge the rate. Rate without denominator is
    the shape that let 14.6% stand unchallenged."""
    from routes.cohort_measurement import _measured_row

    row = _measured_row("cohort.grid_first", {
        "calls_cur": 9, "calls_prior": 4, "agents_cur": 3,
        "agents_prior": 2, "returning_agents": 1, "days": 7}, True)
    for field in ("calls", "agents", "prior_agents", "returning_agents"):
        assert isinstance(row[field], int), field
