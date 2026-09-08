"""call_shape classifies a caller by what it DID, not what it calls itself.

client_class reads the self-declared name. That could not settle the question
it was built for: `connectors-manager` reads like plumbing and behaves like an
agent doing real work. Shape is the stronger signal.

These tests pin the MEASURED cases — real per-tool distributions over 30d to
2026-09-08 — so a threshold cannot be nudged without a case that actually moves
proving it.

★ PROVENANCE. The vectors below were first measured on mcp_call_log. The
endpoint now derives shapes from mcp_calls_identity with PLATFORM_CASE, because
that is the table and classifier per_platform.platform_id comes from and
matching two id spaces by string equality left 6 of 16 rows unshaped. The
boundary cases SURVIVED that change, which is the real check on the threshold:

    smithery connect      max/median 3.58   (was 3.52 on mcp_call_log)
    connectors-manager    max/median 5.00   (unchanged)

Still either side of 4.0 under a different table, a different classifier and a
different traffic filter. `chatgpt` lands on exactly 4.00 and is a SWEEP by the
<= boundary — a genuine tie, recorded rather than tuned away.
"""
from __future__ import annotations

import ai_platform_canon as canon

# Real distributions, top counts verbatim from the measurement.
CHAIN_HIRE      = [1473, 2]                       # search hammered
FABRIQUE        = [60]                            # one tool, only tool
ACTIONIST       = [15, 9, 6, 6, 6, 6, 5, 5, 5, 5, 5, 5, 5, 5, 4]
SMITHERY        = [190, 189, 118, 105, 88, 87] + [60] * 31
REPUTATION_SCAN = [7, 7, 7, 7, 7]
CONNECTORS_MGR  = [15, 15, 13, 6, 5, 5, 4, 4, 4, 4, 3, 3, 3,
                   2, 2, 2, 1, 1, 1, 1, 1, 1, 1]
CLAUDE          = [1486, 53, 30, 29, 25, 15] + [3] * 46
MCP             = [6632, 784, 452, 402, 366, 298] + [20] * 75


def test_a_hammered_tool_is_single_tool():
    assert canon.call_shape(CHAIN_HIRE) == canon.SHAPE_SINGLE_TOOL
    assert canon.call_shape(FABRIQUE) == canon.SHAPE_SINGLE_TOOL


def test_a_flat_catalogue_walk_is_a_sweep():
    for v in (ACTIONIST, SMITHERY, REPUTATION_SCAN):
        assert canon.call_shape(v) == canon.SHAPE_SWEEP, v[:6]


def test_a_steep_distribution_is_a_real_workload():
    for v in (CONNECTORS_MGR, CLAUDE, MCP):
        assert canon.call_shape(v) == canon.SHAPE_VARIED, v[:6]


def test_the_threshold_sits_inside_the_measured_gap():
    """★ THE WHOLE CLASSIFIER RESTS ON THIS ONE CONSTANT.

    smithery (max/median 3.52) is the flattest thing that is arguably tooling;
    connectors-manager (5.00) is the steepest thing that is arguably an agent.
    _FLAT_MAX_OVER_MEDIAN must separate exactly those two, or it is separating
    something other than what was measured. Two samples either side is thin —
    the point of pinning both is that moving the constant breaks a real case
    rather than a made-up one.
    """
    assert canon.call_shape(SMITHERY) == canon.SHAPE_SWEEP
    assert canon.call_shape(CONNECTORS_MGR) == canon.SHAPE_VARIED
    lo = canon.shape_stats(SMITHERY)["max_over_median"]
    hi = canon.shape_stats(CONNECTORS_MGR)["max_over_median"]
    assert lo < canon._FLAT_MAX_OVER_MEDIAN < hi, (
        "the threshold %s no longer sits between the two boundary cases "
        "(smithery %s, connectors-manager %s)"
        % (canon._FLAT_MAX_OVER_MEDIAN, lo, hi))


def test_claude_is_single_tool_dominant_and_still_varied():
    """The closest real call on the OTHER threshold: 1,486 of 1,776 calls on
    one tool is 0.837 — under the 0.90 bar, so it stays varied. An assistant
    can lean hard on one tool without being a scraper, and this is the case
    that would break first if _SINGLE_TOOL_SHARE were lowered."""
    st = canon.shape_stats(CLAUDE)
    assert st["top_share"] > 0.80, st
    assert st["top_share"] < canon._SINGLE_TOOL_SHARE, st
    assert st["shape"] == canon.SHAPE_VARIED


def test_too_few_calls_is_not_a_shape():
    """★ Forcing 4 calls into a bucket manufactures a verdict out of noise —
    the same failure as reading `unknown` as `tooling`."""
    for v in ([2, 2], [4], [1, 1, 1], [], [0, 0]):
        assert canon.call_shape(v) == canon.SHAPE_INSUFFICIENT, v
    assert canon.SHAPE_INSUFFICIENT not in (
        canon.SHAPE_SWEEP, canon.SHAPE_VARIED, canon.SHAPE_SINGLE_TOOL)


def test_garbage_input_is_insufficient_never_a_shape():
    for v in (None, "nonsense", {"a": None}, [None, None]):
        assert canon.call_shape(v) == canon.SHAPE_INSUFFICIENT, v


def test_it_accepts_a_tool_name_mapping_as_well_as_counts():
    d = {"search": 1473, "research_task": 2}
    assert canon.call_shape(d) == canon.call_shape(CHAIN_HIRE)


def test_stats_publish_the_numbers_the_verdict_rests_on():
    """A classifier that shows only its answer cannot be argued with."""
    st = canon.shape_stats(CONNECTORS_MGR)
    for k in ("calls", "distinct_tools", "top_share", "max_over_median", "shape"):
        assert k in st, k
    assert st["calls"] == sum(CONNECTORS_MGR)
    assert st["distinct_tools"] == len(CONNECTORS_MGR)
    assert st["shape"] == canon.call_shape(CONNECTORS_MGR), (
        "shape_stats and call_shape disagree — two implementations of one rule")


# ── wiring: the classifier must actually run on the endpoint ──────────────
def test_reach_calls_the_classifier_and_publishes_the_split():
    """★ A classifier nothing calls is registered, not functional.

    ai_reach must pass real shapes into _stamp_vendor at every call site and
    publish the rollup — otherwise call_shape is a unit-tested function that
    changes nothing anyone can read.
    """
    import ast
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "routes" / "ai_reach.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "_stamp_vendor"]
    assert calls, "_stamp_vendor is never called"
    for c in calls:
        assert len(c.args) >= 2, (
            "a _stamp_vendor call site passes no shapes, so every row there "
            "reports insufficient_data regardless of what the caller did")

    assert "_call_shapes()" in src, "the shape query is never run"
    assert '"call_shape_error"' in src, (
        "a failed shape query would publish an empty split with no reason — "
        "'measured, and there is nothing there' is the worst thing a payload "
        "can say by accident")


def test_the_shape_query_is_isolated_from_the_main_transaction():
    """★ In Postgres a timed-out statement aborts the whole transaction. The
    heaviest read on this endpoint must not be able to degrade every other key
    in the payload, so it opens its own connection."""
    import ast
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "routes" / "ai_reach.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_call_shapes"), None)
    assert fn is not None, "_call_shapes not found"
    body = ast.get_source_segment(src, fn) or ""
    assert "psycopg2.connect" in body, (
        "_call_shapes reuses the caller's connection; a statement timeout "
        "there would poison every query after it")
    assert "statement_timeout" in body, "the heaviest read has no timeout"
    assert "finally" in body and "close()" in body, (
        "_call_shapes can leak its connection on the error path")


def test_the_shape_sql_uses_no_percent_formatting_at_all():
    """★ PLATFORM_CASE CARRIES LITERAL % AND IS NOW INLINED HERE.

    Its ILIKE patterns contain %, so ANY `sql % value` over a string holding it
    raises "unsupported format character" and takes the endpoint down — the
    exact outage this repo has already had twice. The window is concatenated as
    a validated int instead.

    Checking string CONSTANTS is not enough any more: PLATFORM_CASE arrives at
    runtime via _PC.strip(), so a literal-scan sees a clean query and the real
    SQL still carries %. This asserts the stronger, checkable property — no
    %-formatting operator anywhere in the function.
    """
    import ast
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "routes" / "ai_reach.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_call_shapes")
    for node in ast.walk(fn):
        assert not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod)), (
            "%-formatting inside _call_shapes: PLATFORM_CASE contains literal %% "
            "and this raises at request time. Concatenate a validated int.")
    assert "PLATFORM_CASE" in src, "the shape query no longer uses the canon classifier"


def test_the_shape_query_reads_the_same_table_and_classifier_as_per_platform():
    """★ ONE ID SPACE, OR THE COLUMN IS DECORATION.

    per_platform.platform_id is PLATFORM_CASE over mcp_calls_identity. The
    first version of the shape query read mcp_call_log.platform — the raw
    self-declared string — and matched the two by string equality. 6 of 16 rows
    found nothing and published insufficient_data with calls=None, which reads
    as "too few calls to judge" and meant "no such id in this table":
    anthropic/api had 165 calls over 27 tools, claude-ai 68, claude-code 31.
    """
    import ast
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "routes" / "ai_reach.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_call_shapes")
    # ★ Inspect the SQL, not the prose. The comment above the query names
    # mcp_call_log on purpose — it records why the table changed — and a guard
    # that reads comments would force that explanation to be deleted to stay
    # green. Only string constants that look like SQL are examined.
    sql = " ".join(
        n.value for n in ast.walk(fn)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and ("SELECT" in n.value.upper() or "FROM " in n.value.upper()
             or "WHERE" in n.value.upper()))
    assert sql.strip(), "no SQL string found in _call_shapes"
    assert "mcp_calls_identity" in sql, (
        "the shape query reads a different table from the one platform_id is "
        "derived from, so the two id spaces cannot be matched")
    assert "mcp_call_log" not in sql, (
        "the shape SQL still reads mcp_call_log — that is the other id space")
    assert "is_real_external" in sql, (
        "the shape query does not apply the same real-traffic filter as the "
        "rows it stamps, so a shape would describe a different population")
    body = ast.get_source_segment(src, fn) or ""
    assert "PLATFORM_CASE" in body, (
        "the shape query does not apply the canonical classifier, so its ids "
        "are raw strings while platform_id is classified")
