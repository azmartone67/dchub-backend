"""Guards on the public benchmark suite.

The whole value of this report is that a reader can trust every cell. These
tests exist to keep the two failure modes that would destroy that:

  1. A row that renders a plausible number where nothing was measured. That
     is the fabricated-activity-feed defect (removed 2026-08-04) reappearing
     in a table nobody would think to check.
  2. A single captured run published under a word that means "many runs" —
     median, average, typical, p50. ChatGPT asked for a median; we hold one
     capture per question, so the field is named for what it is.

Assertions run against COMMENT-STRIPPED source where they scan text: three
separate tests in this repo have passed by matching the comment that
explained the bug they were meant to catch.
"""
from __future__ import annotations

import ast
import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parents[1] / "routes" / "canonical_benchmarks.py"


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


def test_module_parses_and_names_resolve():
    """An empty or unparseable module would pass every text scan below."""
    raw = SRC.read_text(encoding="utf-8")
    tree = ast.parse(raw)
    funcs = {n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert {"_row_for", "_execute_plan_p50", "canonical_benchmarks"} <= funcs, (
        f"expected functions missing — module may have been gutted: {funcs}")


def test_never_run_rows_carry_nulls_not_numbers():
    """A contracted question with no capture must publish nulls.

    Reads the real function rather than trusting the source text.
    """
    import sys
    sys.path.insert(0, str(SRC.resolve().parents[1]))
    from routes.canonical_benchmarks import _row_for  # noqa: E402

    row = _row_for({"intent": "a question we have never run",
                    "expect_class": "market_ranking"}, None)
    assert row["status"] == "never_run"
    assert row["maturity"] == "unproven"
    for field in ("last_successful_execution", "workflow_ms",
                  "steps_planned", "steps_executed", "coverage"):
        assert row[field] is None, (
            f"{field} must be null on a never-run row — a plausible value "
            "here is the fabricated-feed defect")


def test_coverage_is_derived_from_deferred_steps():
    """coverage/maturity are computed, never hand-assigned."""
    import sys
    sys.path.insert(0, str(SRC.resolve().parents[1]))
    from routes.canonical_benchmarks import _row_for  # noqa: E402

    contract = {"intent": "q", "expect_class": "fiber"}
    complete = _row_for(contract, {
        "intent": "q", "intent_class": "fiber", "ms": 1234, "steps": 2,
        "executed": [{"tool": "a", "status": "executed"},
                     {"tool": "b", "status": "executed"}]})
    assert complete["coverage"] == "complete"
    assert complete["maturity"] == "mature"
    assert complete["deferred_steps"] == []

    partial = _row_for(contract, {
        "intent": "q", "intent_class": "fiber", "ms": 1234, "steps": 2,
        "executed": [{"tool": "a", "status": "executed"},
                     {"tool": "b", "status": "deferred"}]})
    assert partial["coverage"] == "partial"
    assert partial["maturity"] == "expanding"
    assert partial["deferred_steps"] == ["b"], (
        "a deferred step must be NAMED — 'partial' without the tool name "
        "tells a reader nothing they can act on")
    assert partial["steps_executed"] == 1


def test_single_capture_is_never_called_a_median():
    """The per-row timing field must not borrow a many-runs word.

    workflow_ms is one run. Calling it p50/median/average/typical would make
    a service-level claim we cannot support from a single capture.
    """
    src = _stripped()
    m = re.search(r'"(\w*(?:median|average|typical|p50|mean)\w*)"\s*:\s*'
                  r'baked\.get\("ms"\)', src, re.I)
    assert m is None, (
        f"the single-capture timing is published as {m.group(1)!r} — that "
        "word promises a distribution we do not have")
    assert '"workflow_ms": baked.get("ms")' in src, (
        "workflow_ms must bind the captured run directly")


def test_p50_withholds_on_a_thin_sample():
    """A median over a handful of calls is a coincidence, not a metric.

    Tested behaviourally, so it breaks if the RULE moves — not merely if
    someone renames the constant.
    """
    import sys
    sys.path.insert(0, str(SRC.resolve().parents[1]))
    from routes.canonical_benchmarks import _MIN_P50_SAMPLES, _p50_verdict

    thin = _p50_verdict(2400, _MIN_P50_SAMPLES - 1)
    assert thin["p50_ms"] is None, (
        "a thin sample must publish null, not the coincidence")
    assert thin["reason"], "withholding must always carry a stated reason"

    ok = _p50_verdict(2400, _MIN_P50_SAMPLES)
    assert ok["p50_ms"] == 2400, "at the floor the median publishes"
    assert ok["reason"] is None

    # Rows exist but the percentile came back null — unknown, not zero.
    nullp = _p50_verdict(None, _MIN_P50_SAMPLES + 100)
    assert nullp["p50_ms"] is None and nullp["reason"], (
        "a null percentile must read as unknown with a reason, never as 0")


def test_p50_reads_the_table_that_has_the_columns():
    """mcp_call_log has tool+duration_ms; the identity view has neither.

    On 2026-08-04 three lanes reported UndefinedColumn as a FINDING for two
    days because `tool` was queried against mcp_calls_identity (which has
    tool_name and no duration column at all). This asserts the exception is
    deliberate so nobody "repoints it at the canonical view".
    """
    import sys
    sys.path.insert(0, str(SRC.resolve().parents[1]))
    from routes.canonical_benchmarks import _P50_TABLE

    assert _P50_TABLE == "mcp_call_log", (
        f"p50 reads {_P50_TABLE!r}; the identity view has no duration_ms and "
        "no `tool` column, so pointing this at it raises UndefinedColumn — "
        "which _safe_lane would then render as a FINDING, not a crash")
    src = _stripped()
    assert "mcp_calls_identity" not in src, (
        "the identity view has no duration_ms and no `tool` column — "
        "querying it here would raise UndefinedColumn and render as a finding")


def test_rows_come_from_the_contract_not_the_bakes():
    """Iterating _BAKED would make a never-run question invisible.

    The contract is the full list of questions we claim to answer; the bakes
    are what succeeded. Rows must be driven by the former so a missing
    capture shows up as a failing row instead of silently vanishing.
    """
    src = _stripped()
    assert re.search(r"for\s+c\s+in\s+_REPLAY_INTENTS", src), (
        "rows must be built by walking _REPLAY_INTENTS")
    assert not re.search(r"rows\s*=\s*\[[^\]]*for\s+\w+\s+in\s+_BAKED", src), (
        "walking _BAKED to build rows hides every question that never ran")


def test_absent_step_status_fails_closed():
    """A step with no `status` must NOT count as executed.

    The original code read `(s.get("status") or "executed")`, so a missing
    status resolved to the flattering literal. If the MCP server ever stopped
    emitting `status`, every step would count as executed and coverage would
    flip partial->complete silently, in the direction that flatters us.
    """
    import sys
    sys.path.insert(0, str(SRC.resolve().parents[1]))
    from routes.canonical_benchmarks import _row_for

    row = _row_for({"intent": "q", "expect_class": "fiber"}, {
        "intent": "q", "intent_class": "fiber", "ms": 100, "steps": 2,
        # second step carries NO status key at all
        "executed": [{"tool": "a", "status": "executed"}, {"tool": "b"}]})
    assert row["steps_executed"] == 1, (
        "a step with no status must not be counted as executed")
    assert row["coverage"] == "partial"
    assert row["maturity"] == "expanding"
    assert row["deferred_steps"] == ["b"], (
        "an unknown-status step must be NAMED, not silently dropped")


def test_p50_excludes_our_own_traffic():
    """The p50 population must carry the canonical externality predicates.

    An audit proved the unfiltered version causally: three execute_plan calls
    under a `dchub-` user-agent raised `samples` 152->155 and moved the
    PUBLISHED p50 by ~190ms. Roughly three quarters of that population was DC
    Hub's own traffic -- including the weekly refresh that builds this table.
    """
    src = _stripped()
    assert "external_platform_predicate" in src, (
        "the platform exclusion is missing -- our own services count as agents")
    assert "real_ua_predicate" in src, (
        "the user-agent exclusion is missing -- scripted/internal callers "
        "count as agents")
    # Imported from the canonical module, never re-typed: a second
    # hand-maintained exclusion list is how regex twins drift apart.
    assert "from mcp_calls_deloop import" in src, (
        "predicates must be imported from the canonical de-loop module")
    # And they must actually reach the executed filters, not merely be
    # imported. Checked behaviourally against the real filter list, so this
    # survives refactors of how the SQL string is assembled.
    import sys
    sys.path.insert(0, str(SRC.resolve().parents[1]))
    from mcp_calls_deloop import external_platform_predicate, real_ua_predicate
    from routes.canonical_benchmarks import _p50_filters

    filters = _p50_filters()
    assert external_platform_predicate("platform") in filters, (
        "the platform exclusion never reaches the query")
    assert real_ua_predicate("user_agent") in filters, (
        "the user-agent exclusion never reaches the query")


def test_unknown_sample_count_is_null_not_zero():
    """db-unavailable means the count is UNKNOWN, not zero.

    `samples: 0` reads as 'we measured and found none' -- a confident claim
    built on a failed query. Same class as the flattering-zero lesson.
    """
    src = _stripped()
    assert '"samples": None' in src, (
        "the initial/unavailable sample count must be None, not 0")


def test_published_population_is_the_query_that_ran():
    """The declared population must be BUILT FROM the executed filters.

    A hand-written description of a filter is a second source of truth, and
    second sources drift: that is exactly how this file's docstring came to
    claim "real external calls" over a population with no externality filter
    at all. The published sql_filters must be the same list the query joins
    into its WHERE, so an edit to one is an edit to both.
    """
    import sys
    sys.path.insert(0, str(SRC.resolve().parents[1]))
    from routes.canonical_benchmarks import _p50_filters, _p50_population

    filters = _p50_filters()
    pop = _p50_population()
    assert pop["sql_filters"] == filters, (
        "the published filters are not the executed filters")
    assert len(filters) >= 5, "a filter was dropped from the population"

    # The externality clauses must be present in what we PUBLISH, not merely
    # applied — a reader has to be able to see them.
    joined = " AND ".join(filters)
    assert "platform" in joined and "user_agent" in joined, (
        "the published population does not disclose its exclusions")

    src = _stripped()
    assert '" AND ".join(_p50_filters())' in src, (
        "the query must join the SAME list that is published; hand-writing "
        "the WHERE separately reintroduces the drift this guard exists for")


def test_population_travels_with_every_p50_outcome():
    """A statistic must declare its population even when it withholds a value.

    A null p50 with no population is unreadable: the reader cannot tell what
    was searched and came up thin.
    """
    import sys
    sys.path.insert(0, str(SRC.resolve().parents[1]))
    from routes.canonical_benchmarks import _MIN_P50_SAMPLES, _p50_verdict

    for n in (0, _MIN_P50_SAMPLES - 1, _MIN_P50_SAMPLES):
        v = _p50_verdict(2400, n)
        assert v.get("population"), (
            f"population missing at n={n} — it must travel with the "
            "statistic in every branch, including the withheld ones")
