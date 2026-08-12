"""Guards on the public Problems Solved report.

This report is CUSTOMER-FACING, which raises the bar above an internal board:
a buyer asks "94% of what, measured how, over how many runs, since when?" and
every cell has to answer without a human in the loop. These tests keep the four
ways a table like this becomes a liability in a sales conversation:

  1. A percentage computed over a handful of runs. A 94% over 7 runs is a
     coincidence wearing a percentage sign.
  2. A problem class that quietly vanishes because it never ran, leaving a
     table that shows only what worked.
  3. A published population description that has drifted from the query that
     actually ran.
  4. A step with no status counting as executed — the flattering default that
     flips partial into complete silently.

Assertions run against COMMENT-STRIPPED source where they scan text: three
separate tests in this repo have passed by matching the comment that explained
the bug they were meant to catch.
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys

SRC = pathlib.Path(__file__).resolve().parents[1] / "routes" / "problems_solved.py"
ROOT = str(SRC.resolve().parents[1])


def _stripped() -> str:
    """Source with comments and docstrings removed."""
    raw = SRC.read_text(encoding="utf-8")
    no_comments = re.sub(r"(?m)#.*$", "", raw)
    tree = ast.parse(raw)
    doc_spans = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            d = ast.get_docstring(node, clean=False)
            if d:
                doc_spans.add(d)
    for d in doc_spans:
        no_comments = no_comments.replace(d, "")
    return no_comments


def _mod():
    sys.path.insert(0, ROOT)
    import routes.problems_solved as m  # noqa: E402
    return m


def test_module_parses_and_names_resolve():
    """An empty or gutted module would pass every text scan below."""
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    funcs = {n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert {"_classify_run", "_rate_verdict", "_median_verdict", "_row_for",
            "_contract_drift", "_run_filters", "problems_solved"} <= funcs, (
        f"expected functions missing — module may have been gutted: {funcs}")


# ── 1 · the taxonomy is the canonical one, not a parallel list ──────────────

def test_problem_list_is_imported_from_the_canonical_taxonomy():
    """Rows must walk the published taxonomy, never a local re-typing.

    The whole point of routes/problem_taxonomy.py is that the problem
    vocabulary has ONE owner; it exists because that vocabulary had already
    drifted across three independent transcriptions.
    """
    src = _stripped()
    assert "from routes.problem_taxonomy import" in src, (
        "the problem list must be imported from the canonical taxonomy module")
    assert re.search(r"for\s+problem\s+in\s+IN_SCOPE", src), (
        "rows must be built by walking IN_SCOPE — the published contract")

    m = _mod()
    from routes.problem_taxonomy import IN_SCOPE
    assert set(m._PROBLEM_CLASSES) == set(IN_SCOPE), (
        "the measurement mapping and the published taxonomy have diverged")


def test_contract_drift_guard_is_clean_and_can_fail():
    """Every published problem is mapped, and classes must PARTITION.

    An intent class mapped to two problems would count one run twice and
    inflate both rows.
    """
    m = _mod()
    assert m._contract_drift() == [], (
        f"contract drift present: {m._contract_drift()}")

    classes = [c for v in m._PROBLEM_CLASSES.values() for c in v]
    assert len(classes) == len(set(classes)), (
        "an intent class serves two problems — one run would be counted twice")

    # The guard must be able to FAIL, or it guards nothing.
    orig = dict(m._PROBLEM_CLASSES)
    try:
        first = next(iter(orig))
        m._PROBLEM_CLASSES.pop(first)
        assert m._contract_drift(), (
            "removing a published problem's mapping did not trip the drift "
            "guard — the guard cannot fail and so guards nothing")
    finally:
        m._PROBLEM_CLASSES.clear()
        m._PROBLEM_CLASSES.update(orig)


# ── 2 · never-run and no-route classes stay visible, carrying nulls ─────────

def test_never_run_problem_stays_a_row_with_nulls():
    """A taxonomy problem with zero runs renders nulls and REMAINS a row.

    Dropping it would let the table silently flatter itself by showing only
    the problems that worked.
    """
    m = _mod()
    row = m._row_for("interconnection queues",
                     {"entry_tool": "get_interconnection_queue",
                      "status": "mature", "limits": ()},
                     None)
    assert row["status"] == "never_run"
    assert row["runs"] == 0
    for field in ("completed_pct", "partial_pct", "failed_pct"):
        assert row[field] is None, (
            f"{field} must be null on a never-run row — a plausible number "
            "here is the fabricated-activity defect")
    assert row["median_workflow"]["median_steps"] is None
    assert row["reason"], "a never-run row must say why it is empty"
    assert "not" in row["reason"].lower() and "0%" in row["reason"], (
        "the reason must distinguish 'no runs' from 'a 0% completion rate'")


def test_no_planner_route_is_distinct_from_never_run():
    """"No route exists" and "a route exists but nobody used it" are different
    findings and must never render identically."""
    m = _mod()
    row = m._row_for("AI/GPU compute campuses",
                     {"entry_tool": "ai_capacity_index", "status": "expanding",
                      "limits": ()}, None)
    assert row["status"] == "no_planner_route"
    assert row["runs"] is None, (
        "a problem with no planner route has an UNDEFINED run count, not 0 — "
        "0 would claim we measured and found none")
    assert row["no_route_reason"], "an unexplained permanent zero is a defect"


def test_every_published_problem_renders_a_row():
    """13 published problems in, 13 rows out — whatever the data says."""
    m = _mod()
    from routes.problem_taxonomy import IN_SCOPE
    cov = m._coverage_by_problem()
    rows = [m._row_for(p, cov.get(p), None) for p in IN_SCOPE]
    assert len(rows) == len(IN_SCOPE)
    assert {r["problem"] for r in rows} == set(IN_SCOPE)


# ── 3 · thin samples withhold, with a reason ───────────────────────────────

def test_thin_sample_withholds_with_a_reason():
    """Below the minimum, every rate is null and the reason is stated.

    Tested behaviourally against the RULE, so it breaks if the rule moves —
    not merely if someone renames the constant.
    """
    m = _mod()
    thin_pct, thin_reason = m._rate_verdict(m._MIN_RUNS - 1, m._MIN_RUNS - 1)
    assert thin_pct is None, (
        "a thin sample must publish null, not the coincidence — even at 100%")
    assert thin_reason, "withholding must always carry a stated reason"
    assert str(m._MIN_RUNS) in thin_reason, (
        "the reason must state the minimum a reader is being held to")

    ok_pct, ok_reason = m._rate_verdict(19, m._MIN_RUNS)
    assert ok_pct == 95.0, "at the floor the rate publishes"
    assert ok_reason is None


def test_withheld_row_publishes_n_but_no_percentage():
    """n is measured, so it publishes. A rate over that n is not, so it does
    not. Withholding the count too would hide the very thinness being
    disclosed."""
    m = _mod()
    agg = {"n": 7, "completed": 6, "partial": 1, "failed": 0, "in_flight": 0,
           "exec_steps": [3] * 7, "deficits": {"skipped_unresolved": 1},
           "gated_runs": 0}
    row = m._row_for("tax incentives and permitting",
                     {"entry_tool": "get_tax_incentives", "status": "mature",
                      "limits": ()}, agg)
    assert row["status"] == "withheld"
    assert row["runs"] == 7, "the measured count must still publish"
    assert row["completed_pct"] is None, (
        "6/7 = 85.7% is exactly the coincidence this rule exists to suppress")
    assert row["reason"] and str(m._MIN_RUNS) in row["reason"]
    assert row["counts_only"]["completed"] == 6, (
        "raw counts must remain visible so a reader can judge for themselves")
    assert row["partial_reasons"], (
        "what was missing is the most actionable thing on a thin row and must "
        "survive withholding")


# ── 4 · a step with no status fails CLOSED ─────────────────────────────────

def test_status_less_step_fails_closed():
    """A step carrying no status must NOT count as executed.

    The gateway writes counts keyed by the step's status; a step with no status
    lands under the literal JS key "undefined". If that counted as executed,
    completion would flip partial->completed silently, in the direction that
    flatters us. This is the canonical_benchmarks 0805 defect in a new table.
    """
    m = _mod()
    verdict, deficits, good = m._classify_run(
        3, {"executed": 2, "undefined": 1}, "completed", 999, 15)
    assert verdict == "partial", (
        "a step with no status must not count toward completion")
    assert good == 2, "only allow-listed statuses count as executed"
    assert "undefined" in deficits, (
        "an unknown-status step must be NAMED, not silently dropped")


def test_unknown_future_status_fails_closed():
    """The allow-list must fail closed on a status this code has never seen.

    A deny-list would fail OPEN the day the gateway adds a status — the whole
    table would silently improve because a new failure mode was unrecognised.
    """
    m = _mod()
    verdict, deficits, good = m._classify_run(
        2, {"executed": 1, "quarantined_by_a_future_release": 1},
        "completed", 999, 15)
    assert verdict == "partial", (
        "an unrecognised status must count as NOT executed")
    assert good == 1
    assert any("quarantined" in d for d in deficits)


def test_run_with_no_status_at_all_is_never_completed():
    """Unverifiable is not complete. An absence of recorded failures is not
    evidence of success — that is the defect the definition exists to bar."""
    m = _mod()
    verdict, deficits, _ = m._classify_run(3, {}, "completed", 999, 15)
    assert verdict == "failed"
    assert "no_step_status_recorded" in deficits


def test_abandoned_run_is_not_completed():
    """A NULL outcome past the abandonment threshold is abandoned.

    The internal derivation this builds on accepts `outcome in ("completed",
    None)`, so a NULL outcome can resolve to complete there. Here it must not.
    """
    m = _mod()
    verdict, deficits, _ = m._classify_run(3, {"executed": 3}, None, 999, 15)
    assert verdict != "completed", (
        "an abandoned workflow is not a closed problem")
    assert any(d.startswith("outcome_") for d in deficits)


def test_in_flight_run_is_excluded_from_the_denominator():
    """A running workflow is neither a success nor a failure."""
    m = _mod()
    verdict, _, _ = m._classify_run(3, {"executed": 3}, None, 2.0, 15)
    assert verdict == "in_flight"


def test_partial_names_what_was_missing():
    """"39% partial" is useless to a buyer; naming the deficit is the asset."""
    m = _mod()
    agg = {"n": 25, "completed": 15, "partial": 10, "failed": 0,
           "in_flight": 0, "exec_steps": [3] * 25,
           "deficits": {"skipped_unresolved": 8, "not_run": 2},
           "gated_runs": 0}
    row = m._row_for("fiber routes, diversity and latency",
                     {"entry_tool": "get_fiber_intel", "status": "mature",
                      "limits": ()}, agg)
    assert row["status"] == "measured"
    assert row["completed_pct"] == 60.0
    top = row["partial_reasons"][0]
    assert top["code"] == "skipped_unresolved" and top["runs_affected"] == 8, (
        "reasons must be ranked by how often they actually bit")
    assert len(top["reason"]) > 30, (
        "a raw status code is not a reason a buyer can act on")


# ── 5 · median is steps EXECUTED, and never a misleading midpoint ──────────

def test_median_is_withheld_on_a_bimodal_distribution():
    """A midpoint between two clusters describes no run that happened."""
    m = _mod()
    v = m._median_verdict([2] * 15 + [6] * 15)
    assert v["median_steps"] is None
    assert v["shape"] == "bimodal"
    assert v["reason"] and "bimodal" in v["reason"]
    assert v["distribution"] == {"2": 15, "6": 15}, (
        "the histogram must publish so a reader can compute their own")


def test_median_publishes_when_unimodal_and_well_sampled():
    m = _mod()
    v = m._median_verdict([3] * 20 + [4] * 5)
    assert v["shape"] == "unimodal"
    assert v["median_steps"] == 3
    assert v["reason"] is None


def test_median_is_over_steps_executed_not_steps_planned():
    """The published definition must say EXECUTED, and the code must agree."""
    m = _mod()
    assert "EXECUTED" in m._DEFINITIONS["median_workflow_steps"]
    assert "Not steps planned" in m._DEFINITIONS["median_workflow_steps"]
    src = _stripped()
    assert re.search(r'exec_steps"\]\.append\(good\)', src), (
        "the median must be fed the count of steps that actually executed")


def test_failed_runs_are_excluded_from_the_median():
    """A failed run's step count measures the failure, not the question."""
    src = _stripped()
    assert re.search(r'if\s+verdict\s*!=\s*"failed":\s*\n\s*b\["exec_steps"\]',
                     src), (
        "failed runs must not feed the median workflow length")


# ── 6 · self-traffic exclusion, imported not re-typed ──────────────────────

def test_population_excludes_our_own_traffic():
    """The canonical externality predicates must be imported AND applied."""
    src = _stripped()
    assert "from mcp_calls_deloop import" in src, (
        "predicates must be imported from the canonical de-loop module — a "
        "second hand-maintained exclusion list is how regex twins drift apart")

    m = _mod()
    from mcp_calls_deloop import external_platform_predicate, real_ua_predicate
    filters = m._run_filters()
    assert external_platform_predicate("platform") in filters, (
        "the platform exclusion never reaches the query")
    assert real_ua_predicate("user_agent") in filters, (
        "the user-agent exclusion never reaches the query")


def test_report_reads_the_source_that_can_actually_exclude_self_traffic():
    """recipe_executions carries user_agent; the call-log rows do not.

    The MCP server's execute_plan_steps tracking payload has no user_agent key
    at all, so user_agent is NULL on every such row and real_ua_predicate --
    which correctly KEEPS NULL for real anonymous agents -- degrades to a
    no-op there. Publishing an exclusion that cannot fire buys credibility it
    has not earned. This pins the source choice so nobody "simplifies" it.
    """
    m = _mod()
    assert m._SOURCE_TABLE == "recipe_executions", (
        f"the report reads {m._SOURCE_TABLE!r}; mcp_call_log carries no usable "
        "user_agent for execute_plan_steps rows, so the UA exclusion would be "
        "a no-op and the published self-traffic filter would be theatre")


def test_published_population_is_the_query_that_ran():
    """The declared population must be BUILT FROM the executed filters."""
    m = _mod()
    filters = m._run_filters()
    pop = m._population()
    assert pop["sql_filters"] == filters, (
        "the published filters are not the executed filters")
    assert len(filters) >= 4, "a filter was dropped from the population"
    joined = " AND ".join(filters)
    assert "platform" in joined and "user_agent" in joined, (
        "the published population does not disclose its exclusions")
    src = _stripped()
    assert '" AND ".join(_run_filters())' in src, (
        "the query must join the SAME list that is published; hand-writing "
        "the WHERE separately reintroduces the drift this guard exists for")


def test_self_traffic_volume_is_published_not_asserted():
    """Both sides of the exclusion must ship, so the claim is checkable."""
    m = _mod()
    src = _stripped()
    for field in ("runs_before_exclusions", "runs_after_exclusions"):
        assert field in src, f"{field} must be published"
    assert "self_traffic_excluded" in src


# ── 7 · unknowns stay unknown ──────────────────────────────────────────────

def test_db_unavailable_reads_as_unknown_not_zero():
    """A flattering zero is a bug. So is a flattering 'never_run'."""
    src = _stripped()
    assert re.search(r'row\["status"\]\s*=\s*"unmeasured"', src), (
        "when measurement fails, rows must read UNMEASURED — rendering them "
        "as never_run would claim we looked and found nothing")

    # And behaviourally: with the DB unreachable, no row may claim a count.
    m = _mod()
    real_conn = m._conn
    try:
        m._conn = lambda: None
        d = m._build()
        assert d["measurement_ok"] is False
        assert d["measurement_reason"], "an unmeasured report must say why"
        routed = [r for r in d["rows"] if r["intent_classes"]]
        assert routed, "fixture sanity: some problems have planner routes"
        for r in routed:
            assert r["status"] == "unmeasured", (
                f"{r['problem']!r} reads {r['status']!r} with the database "
                "down — that claims we measured and found nothing")
            assert r["runs"] is None, (
                "runs must be UNKNOWN (null), never 0, when nothing was read")
            assert r["completed_pct"] is None
    finally:
        m._conn = real_conn


def test_unattributed_classes_are_published_not_dropped():
    """A silently discarded intent class is a denominator quietly shrinking."""
    src = _stripped()
    assert "unattributed_classes" in src
    m = _mod()
    d = m._build()
    assert "unattributed_classes" in d and "unattributed_note" in d


def test_constraint_check_gap_is_disclosed():
    """One of the planner's two constraint_checks is not evaluable here.

    Publishing "the constraint_check passed" while one of them was never
    evaluated is the flattering-unknown defect. The gap must be named.
    """
    m = _mod()
    cc = m._CONSTRAINT_COVERAGE
    assert cc["evaluated"] and cc["not_evaluated"], (
        "both the evaluated and the unevaluated constraint checks must ship")
    assert any(x["id"] == "C1" for x in cc["not_evaluated"]), (
        "the geography check is not evaluable on this source and must say so")
    assert any(x["id"] == "C2" for x in cc["evaluated"])


def test_completed_definition_is_positive_evidence_not_absence_of_errors():
    """The definition ships IN the payload and forbids the lazy reading."""
    m = _mod()
    d = m._DEFINITIONS["completed"]
    assert "EVERY PLANNED STEP EXECUTED" in d
    assert "never from an absence of errors" in d
    assert "steps_planned" in d, (
        "the definition must be operational enough to recompute from raw "
        "fields, not a slogan")


def test_report_builds_end_to_end_without_a_database():
    """The endpoint must degrade to nulls-with-reasons, never raise."""
    m = _mod()
    from routes.problem_taxonomy import IN_SCOPE
    d = m._build()
    assert d["report"] == "problems-solved"
    # Checked against the CONTRACT, not against the rendered rows — comparing
    # rows to a count derived from rows is a tautology that a dropped row
    # would satisfy.
    assert d["summary"]["problems_published"] == len(IN_SCOPE)
    assert len(d["rows"]) == len(IN_SCOPE), (
        "a published problem vanished from the table — that is exactly how "
        "this report would come to show only what worked")
    assert {r["problem"] for r in d["rows"]} == set(IN_SCOPE)
    assert d["summary"]["every_problem_rendered"] is True
    assert d["taxonomy_source"]["module"].endswith("routes/problem_taxonomy.py")
    assert d["minimum_runs_to_publish_a_rate"] == m._MIN_RUNS
    # Every row carries a status; none is silently blank.
    assert all(r["status"] for r in d["rows"])
    html = m._render_html(d)
    assert "Problems Solved" in html and "<table>" in html
