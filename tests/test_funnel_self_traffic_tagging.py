"""OUR OWN SMOKE SUITE COUNTED AS 36% OF PAYWALL DEMAND — the fence.

★ Why this exists (measured 2026-08-17 on the Neon replica, 30d window).

`mcp_funnel_canonical.is_synthetic` decides what every funnel reader counts, and
it keyed exclusively on `mcp_upgrade_signals.mcp_client`. That column is the
DEGRADED copy of the caller's identity:

    signal_type            signals   carrying the literal generic 'mcp'
    trial_preview            5,255   82.2%   <- 84% of all signals
    paid_tool_blocked        1,007   38.0%   (412 correctly 'dchub-internal')

So the whole synthetic filter removed 463 of 6,267 signals and exactly ONE of
835 callers. It was a no-op, and the cost was not academic:

  * dchub-mcp-server `test/regression.test.mjs` points at LIVE dchub.cloud/mcp
    and runs on every push, every PR, and a 13:00 UTC cron. `caller_id` falls
    back to `session_id`, and every run opens a fresh MCP session, so the suite
    read as **112 distinct "callers" / 2,240 paywall signals = 36% of demand**,
    in a recognisable shape: exactly 20 hits, 13 tools, one day, one hour,
    byte-identical tool ORDER, fixed demo args.
  * A conversion handoff then read the resulting 20-49-hit cohort as "the
    customers who pay for our data" and proposed working it as a lead list.

The identity was never lost — `mcp_call_log.platform` resolves it correctly
(server.mjs `detectPlatformFromInit` maps a clientInfo.name matching
/regression|probe|verify|harness|.../ to 'dchub-internal'). Only the signal
row's copy degrades. So `self_traffic` is recovered from call_log at SESSION
level and stored on the row.

Design, per this repo's rules: every test here is STATIC and PURE. CI sets no
DATABASE_URL, so a DB-backed assertion would either error or be skipped into
silent green. The shipped source is parsed with `ast` — never grep, which passes
happily on a COMMENT that mentions the right token, a bug this repo has shipped
before. Every helper below asserts it actually FOUND its target, because an
empty parse satisfies every `not in`.
"""
from __future__ import annotations

import ast
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIGNAL_SRC = os.path.join(REPO, "mcp_signal_canonical.py")
SCHEMA_SRC = os.path.join(REPO, "routes", "schema_repair.py")


def _tree(path):
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    assert src.strip(), f"{path} is empty — this file would pass vacuously"
    return src, ast.parse(src)


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(
        f"function {name}() NOT FOUND — the guard lost its target, so every "
        f"assertion below would be vacuous")


def _string_constants(node):
    """Every string literal inside a node — comments are NOT included, which is
    the whole point of using ast here rather than grep."""
    return [n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def test_resolver_exists_and_is_called_by_the_only_writer():
    _, tree = _tree(SIGNAL_SRC)
    _func(tree, "_resolve_self_traffic")          # asserts it exists
    record = _func(tree, "record_signal")
    called = {n.func.id for n in ast.walk(record)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_resolve_self_traffic" in called, (
        "record_signal() is THE only path that writes mcp_upgrade_signals; if it "
        "does not call _resolve_self_traffic the flag is never set on new rows "
        "and the fix decays to whenever someone remembers to POST "
        "/api/v1/admin/schema/repair")


def test_insert_column_count_matches_placeholders_and_params():
    """Adding self_traffic to the INSERT means touching three parallel lists.
    A mismatch breaks EVERY signal write, so pin the arithmetic."""
    _, tree = _tree(SIGNAL_SRC)
    record = _func(tree, "record_signal")
    insert = None
    for s in _string_constants(record):
        if "INSERT INTO mcp_upgrade_signals" in s:
            insert = s
            break
    assert insert, "the INSERT INTO mcp_upgrade_signals statement was NOT FOUND"

    def _paren_group(text, start):
        """The balanced (...) group beginning at or after `start`. Needed because
        the VALUES clause contains NOW(), so splitting on the first ')' would
        truncate mid-clause and undercount."""
        i = text.index("(", start)
        depth = 0
        for j in range(i, len(text)):
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
                if depth == 0:
                    return text[i + 1:j]
        raise AssertionError("unbalanced parentheses in the INSERT statement")

    cols_part = _paren_group(insert, insert.index("mcp_upgrade_signals"))
    columns = [c.strip() for c in cols_part.split(",") if c.strip()]
    values_part = _paren_group(insert, insert.index("VALUES"))
    placeholders = values_part.count("%s")
    now_literals = values_part.count("NOW()")

    assert "self_traffic" in columns, (
        f"self_traffic missing from the INSERT column list: {columns}")
    assert len(columns) == placeholders + now_literals, (
        f"{len(columns)} columns vs {placeholders} %s placeholders + "
        f"{now_literals} NOW() — the INSERT is misaligned and every signal "
        f"write would fail")


def test_resolver_escapes_the_literal_percent():
    """psycopg2 interpolates %; a literal % in a parameterised query must be %%
    or the driver raises IndexError/ValueError at execute time. This repo has
    been bitten by exactly this."""
    _, tree = _tree(SIGNAL_SRC)
    fn = _func(tree, "_resolve_self_traffic")
    sql = [s for s in _string_constants(fn) if "mcp_call_log" in s]
    assert sql, "no SQL literal against mcp_call_log found in _resolve_self_traffic"
    joined = " ".join(sql)
    assert "LIKE 'dchub-%%'" in joined, (
        "the LIKE pattern must escape its percent as 'dchub-%%' in a "
        f"parameterised execute(); found: {joined!r}")


def test_resolver_fails_open_never_raises():
    """A telemetry classifier must never break the signal write."""
    _, tree = _tree(SIGNAL_SRC)
    fn = _func(tree, "_resolve_self_traffic")
    handlers = [h for n in ast.walk(fn) if isinstance(n, ast.Try) for h in n.handlers]
    assert handlers, "_resolve_self_traffic has no except handler — a DB blip " \
                     "would propagate into record_signal and lose the signal"
    returns_false = any(
        isinstance(st, ast.Return) and isinstance(st.value, ast.Constant)
        and st.value.value is False
        for h in handlers for st in ast.walk(h))
    assert returns_false, (
        "the except handler must `return False` (fail OPEN — an unclassified "
        "row is one the backfill picks up; failing closed would silently "
        "exclude real demand)")


def _ddl_statements():
    """Every SQL string in the funnel block of schema_repair.py."""
    src, tree = _tree(SCHEMA_SRC)
    stmts = [s for s in _string_constants(tree)
             if "mcp_funnel" in s or "mcp_upgrade_signals" in s
             or "mcp_call_log" in s]
    assert stmts, "no funnel DDL statements found in routes/schema_repair.py"
    return stmts


def test_view_ors_self_traffic_into_is_synthetic():
    stmts = _ddl_statements()
    canonical = [s for s in stmts if "CREATE OR REPLACE VIEW mcp_funnel_canonical" in s]
    assert canonical, "the mcp_funnel_canonical view DDL was NOT FOUND"
    ddl = canonical[0]
    assert "self_traffic IS TRUE" in ddl, (
        "is_synthetic must consider the materialised self_traffic column, or it "
        "stays the mcp_client-only no-op that let our own CI count as demand")
    # The self_traffic branch has to sit INSIDE the is_synthetic CASE, before it
    # is aliased — otherwise it classifies nothing.
    assert ddl.index("self_traffic IS TRUE") < ddl.index("AS is_synthetic"), (
        "the self_traffic branch must appear before `AS is_synthetic` — "
        "otherwise it is not part of that CASE expression")


def test_self_traffic_column_and_backfill_ship_together():
    stmts = _ddl_statements()
    assert any("ADD COLUMN IF NOT EXISTS self_traffic" in s for s in stmts), \
        "the self_traffic column is never created"
    backfill = [s for s in stmts
                if "UPDATE mcp_upgrade_signals" in s and "self_traffic = TRUE" in s]
    assert backfill, (
        "history must be backfilled — without it the 30d funnel keeps counting "
        "the CI runs already in the table")
    assert "EXISTS" in backfill[0] and "mcp_call_log" in backfill[0], (
        "the backfill must recover identity from mcp_call_log at SESSION level "
        "via EXISTS; platform resolution is per-request and flaky, so one CI "
        "session carries both 'mcp' and 'dchub-internal' rows")
    assert any("idx_mcp_call_log_session_id" in s for s in stmts), (
        "mcp_call_log is 696k rows / 356MB with no session_id index — the "
        "backfill needs one or it seq-scans")


def test_registry_probe_is_its_own_dimension_not_folded_into_synthetic():
    """Smithery is a registry AND a hosted gateway: 420 callers / 937 signals
    look like a prober (avg 2.2 hits, 0 conversions, fixed demo args) but 15
    bound emails came through it. Excluding it outright would delete real
    demand, so it must be reported separately, not subtracted silently."""
    stmts = _ddl_statements()
    canonical = [s for s in stmts if "CREATE OR REPLACE VIEW mcp_funnel_canonical" in s]
    assert canonical, "the mcp_funnel_canonical view DDL was NOT FOUND"
    ddl = canonical[0]
    assert "AS is_registry_probe" in ddl, "is_registry_probe column missing"

    # is_synthetic must NOT swallow the registry class.
    synthetic_case = ddl[ddl.index("AS is_registry_probe"):ddl.index("AS is_synthetic")]
    assert "smithery" not in synthetic_case.lower(), (
        "smithery must not be folded into is_synthetic — mcp_funnel_real feeds "
        "existing readers, and 15 real bound emails arrived through that "
        "gateway")

    real = [s for s in stmts if "CREATE OR REPLACE VIEW mcp_funnel_real" in s]
    demand = [s for s in stmts if "CREATE OR REPLACE VIEW mcp_funnel_demand" in s]
    assert real, "mcp_funnel_real view missing"
    assert demand, (
        "mcp_funnel_demand (excludes BOTH ours and catalog crawlers) must exist "
        "so the stricter number is available without redefining what every "
        "current reader of mcp_funnel_real means")
    assert "is_registry_probe = FALSE" in demand[0], \
        "mcp_funnel_demand must exclude registry probes"
    assert "is_registry_probe" not in real[0], \
        "mcp_funnel_real must keep its existing semantics (is_synthetic only)"


def test_views_dropped_in_dependency_order():
    """mcp_funnel_callers and mcp_funnel_demand depend on mcp_funnel_real, which
    depends on mcp_funnel_canonical. Dropping the base first without CASCADE
    ordering makes the repair endpoint fail midway and leave the funnel with no
    views at all."""
    stmts = _ddl_statements()
    drops = [s for s in stmts if s.strip().startswith("DROP VIEW")]
    assert drops, "no DROP VIEW statements found"
    order = {}
    for i, s in enumerate(drops):
        for v in ("mcp_funnel_demand", "mcp_funnel_callers",
                  "mcp_funnel_real", "mcp_funnel_canonical"):
            if v in s and v not in order:
                order[v] = i
    for dependent in ("mcp_funnel_demand", "mcp_funnel_callers", "mcp_funnel_real"):
        assert dependent in order, f"{dependent} is never dropped before recreate"
        assert order[dependent] < order["mcp_funnel_canonical"], (
            f"{dependent} must be dropped BEFORE mcp_funnel_canonical")
