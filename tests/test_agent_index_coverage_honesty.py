"""/api/v1/agent/index coverage-honesty fence — 2026-08-01.

THE BUG THIS FENCES
-------------------
`GET /api/v1/agent/index` served this, HTTP 200, well-formed, no error field,
verified live against Railway-direct:

    "coverage": {"countries_covered": [], "dcpi_scored_markets": 0,
                 "discovered_facilities": 0, "facilities": 0,
                 "ma_transactions": 0, "news_articles": 0,
                 "pipeline_projects": 0, "pocket_listings": 0}

Every number was false. Live: 18,427 / 23,315 / 1,248 / 317 / 2,891 / 1,843 /
0 / 178 countries. This is an AI-agent-facing endpoint whose entire job is
telling agents what data exists, so the zeros actively told every calling
agent we hold nothing.

THE CHAIN (measured, not guessed)
---------------------------------
1. `agent_index()` said `with _conn() as c:`. psycopg2's connection context
   manager is a TRANSACTION manager, not a closer -- entering it opens an
   explicit transaction that `autocommit = True` does NOT override. The
   attribute keeps reporting True while the session sits in
   TRANSACTION_STATUS_INTRANS, so the lie is invisible from Python. The
   module's central design comment ("each cursor's queries are independent
   transactions, so a failure in one section can't poison the others") was
   therefore false in exactly the case it was written for.
2. `_radar_issues` selected `last_seen_at`, `severity` and `note` from
   `data_domain_freshness` -- three columns that table has never had. It threw
   UndefinedColumn on every request since it shipped, `_safe_fetchall` ate the
   exception, and `radar: []` published "no freshness issues" as a fact. (With
   the query fixed it immediately reported a real one: news 26.58h vs 24h SLA.)
3. That failure put the shared transaction in INERROR. A transaction belongs to
   the CONNECTION, so the per-section cursors bought nothing: all eight coverage
   queries then died with InFailedSqlTransaction.
4. `_safe_fetchall` returned [] for each, and `count()` mapped [] to 0.

Steps 1-3 are ordinary bugs. Step 4 is the one that made it invisible for
months, and it is what this file exists to prevent: a read that FAILED and a
read that legitimately returned ZERO are not the same answer, and a coverage
inventory that cannot tell them apart will publish a confident lie.

WHAT IS CHECKED
---------------
* A failed count is `None` + a named entry in `coverage_errors` -- never 0.
* One failed query does not cascade into the rest (the poisoned-transaction
  case, emulated with real Postgres abort semantics).
* `with _conn()` does not come back in any request path.
* The dead columns and the never-existed `transactions` table stay gone.
* `countries_covered` reads discovered_facilities (178, canon), not
  facilities (186, the legacy-186 class closed by #1958/#1966).
* Anti-vacuous floors, per the #2062 lesson: a fence that goes green because
  the thing it inspects became empty is worse than no fence.
"""
import ast
import inspect
import os
import re

import pytest

ai = pytest.importorskip("routes.agent_index")


# ---------------------------------------------------------------- fake driver

class _FakeConn:
    def __init__(self):
        self.rollbacks = 0
        self.aborted = False

    def rollback(self):
        self.rollbacks += 1
        self.aborted = False


class _FakeCursor:
    """psycopg2 semantics INSIDE an explicit transaction.

    Once a statement errors the connection is poisoned: every later statement
    raises InFailedSqlTransaction until somebody rolls back. This is what the
    live connection was doing, and emulating it is the only way to prove the
    cascade is actually fixed rather than merely absent from one code path.
    """

    class Error(Exception):
        pass

    def __init__(self, fail_substrings=(), rows=None, poison=True):
        self.connection = _FakeConn()
        self.fail_substrings = tuple(fail_substrings)
        self.rows = rows or {}
        self.poison = poison
        self.executed = []
        self._result = []

    def execute(self, sql, params=()):
        flat = " ".join(sql.split())
        self.executed.append(flat)
        if self.connection.aborted:
            raise self.Error("current transaction is aborted, commands "
                             "ignored until end of transaction block")
        if any(s in flat for s in self.fail_substrings):
            if self.poison:
                self.connection.aborted = True
            raise self.Error('relation/column does not exist')
        for key, val in self.rows.items():
            if key in flat:
                self._result = val
                return
        self._result = [(0,)]

    def fetchall(self):
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_LIVE_ROWS = {
    "FROM facilities": [(18427,)],
    "COUNT(*) FROM discovered_facilities": [(23315,)],
    "FROM capacity_pipeline": [(1248,)],
    "FROM market_power_scores": [(317,)],
    "FROM news": [(2891,)],
    "FROM deals": [(1843,)],
    "FROM exclusive_listings": [(0,)],
    "DISTINCT country FROM discovered_facilities": [("US",), ("DE",), ("JP",)],
}

_COUNT_KEYS = ["facilities", "discovered_facilities", "pipeline_projects",
               "dcpi_scored_markets", "news_articles", "ma_transactions",
               "pocket_listings"]


def _source_of(fn):
    return inspect.getsource(fn)


def _module_path():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "routes", "agent_index.py")


def _module_tree():
    with open(_module_path(), encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _func_node(name):
    for node in ast.walk(_module_tree()):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name}() not found in routes/agent_index.py")


def _sql_literals(fn_name):
    """Every string constant inside a function, EXCLUDING docstrings.

    AST, not a text scan: prose explaining a trap must never be mistaken for
    the trap itself. All three of this fence's early red runs were exactly
    that -- it flagged the comment warning against `with _conn()`, the comment
    warning against a literal %, and finally _radar_issues' own docstring,
    which names the three dead columns in order to warn about them.
    """
    fn = _func_node(fn_name)
    docstrings = set()
    for node in ast.walk(fn):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            docstrings.add(id(body[0].value))

    return [node.value for node in ast.walk(fn)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in docstrings]


# ------------------------------------------------------- the core requirement

def test_total_db_failure_is_null_never_zero():
    """Every query fails -> every count is None, not 0."""
    cur = _FakeCursor(fail_substrings=("SELECT",))
    out = ai._coverage_summary(cur)

    assert out.get("complete") is False, "a fully failed read must not claim complete"
    assert out.get("coverage_errors"), "a fully failed read must name its errors"

    zeros = {k: v for k, v in out.items() if v == 0 and k != "complete"}
    assert not zeros, (
        "_coverage_summary mapped a QUERY FAILURE to 0. A zero on this endpoint "
        f"tells an AI agent we hold no data at all. Offending keys: {zeros}")

    for key in _COUNT_KEYS:
        assert out[key] is None, f"{key} should be None on failure, got {out[key]!r}"
    assert out["countries_covered"] is None
    assert set(out["coverage_errors"]) >= set(_COUNT_KEYS) | {"countries_covered"}


def test_partial_failure_keeps_good_counts_and_nulls_the_bad():
    """A failure in ONE table must not zero -- or hide -- the others."""
    cur = _FakeCursor(fail_substrings=("FROM news",), rows=_LIVE_ROWS)
    out = ai._coverage_summary(cur)

    assert out["news_articles"] is None, "the failed count must be null"
    assert out.get("complete") is False
    assert "news_articles" in out.get("coverage_errors", {})

    # ...and every other count still came through with its real value.
    assert out["facilities"] == 18427
    assert out["discovered_facilities"] == 23315
    assert out["pipeline_projects"] == 1248
    assert out["dcpi_scored_markets"] == 317
    assert out["ma_transactions"] == 1843
    assert len(out["countries_covered"]) == 3


def test_one_failure_does_not_cascade_through_a_poisoned_transaction():
    """THE regression fence for the live bug.

    First query fails and poisons the transaction, exactly as Postgres does.
    If _try_fetchall does not roll back, every later query dies with
    InFailedSqlTransaction and the whole block renders as zeros -- which is
    precisely what production served.
    """
    cur = _FakeCursor(fail_substrings=("FROM facilities",),
                      rows=_LIVE_ROWS, poison=True)
    out = ai._coverage_summary(cur)

    assert cur.connection.rollbacks > 0, (
        "a failed query left the transaction aborted -- _try_fetchall must "
        "roll back so the NEXT query can still run")

    assert out["facilities"] is None
    survivors = {k: out[k] for k in _COUNT_KEYS if k != "facilities"}
    assert all(v is not None for v in survivors.values()), (
        f"one failed query cascaded into the rest: {survivors}")
    assert out["discovered_facilities"] == 23315
    assert out["ma_transactions"] == 1843


def test_healthy_read_is_complete_and_carries_no_error_block():
    cur = _FakeCursor(rows=_LIVE_ROWS)
    out = ai._coverage_summary(cur)
    assert out.get("complete") is True
    assert "coverage_errors" not in out
    assert out["facilities"] == 18427
    # A genuine zero is still allowed to be zero -- exclusive_listings really
    # is empty live. Honesty runs both ways.
    assert out["pocket_listings"] == 0


def test_zero_and_failure_are_distinguishable():
    """The property the old code structurally could not express."""
    genuine = ai._coverage_summary(_FakeCursor(rows=_LIVE_ROWS))
    broken = ai._coverage_summary(_FakeCursor(fail_substrings=("FROM exclusive_listings",),
                                              rows=_LIVE_ROWS))
    assert genuine["pocket_listings"] == 0
    assert broken["pocket_listings"] is None
    assert genuine["pocket_listings"] != broken["pocket_listings"], (
        "an empty table and an unreadable table must not serialize identically")


# --------------------------------------------------------- structural fences

def test_no_with_conn_in_any_request_path():
    """`with _conn()` is the psycopg2 transaction-manager trap that caused this.

    It silently defeats autocommit, so a single failed query poisons every
    later query on the connection -- across cursors.
    """
    tree = _module_tree()
    offenders, conn_calls = [], 0

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "_conn":
            conn_calls += 1
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                expr = item.context_expr
                if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) \
                        and expr.func.id == "_conn":
                    offenders.append(getattr(node, "lineno", "?"))

    assert not offenders, (
        f"`with _conn()` is back at line(s) {offenders}. psycopg2's connection "
        "context manager opens an explicit transaction that autocommit does NOT "
        "override, so one failed query renders every later count as 0. "
        "Use try/finally + close().")

    # Anti-vacuous: the file must still actually open connections, or the check
    # above passes trivially on a file that no longer talks to the database.
    assert conn_calls >= 2, "no _conn() call sites left to protect"


def test_dead_columns_and_dead_table_stay_gone():
    """The three never-existed columns and the never-existed table."""
    radar_sql = [s for s in _sql_literals("_radar_issues")
                 if "data_domain_freshness" in s]
    assert radar_sql, "_radar_issues no longer queries data_domain_freshness"
    blob = " ".join(radar_sql)
    for dead in ("last_seen_at", "severity", "note"):
        assert not re.search(rf"\b{dead}\b", blob), (
            f"_radar_issues selects `{dead}` again. data_domain_freshness has "
            "domain, source_table, source_ts_column, last_record_at, row_count, "
            "sla_hours, age_hours, status, detail, checked_at -- and nothing else. "
            "A dead column throws, gets swallowed, publishes `radar: []` as "
            "'no issues', AND aborts the shared transaction.")
    assert "last_record_at" in blob, "radar must read the real timestamp column"

    cov_sql = " ".join(_sql_literals("_coverage_summary"))
    assert not re.search(r"FROM\s+transactions\b", cov_sql), (
        "`transactions` is back. That table has never existed in this database; "
        "the live M&A table is `deals`. Counting it is a hard 0 forever.")
    tables = [s for s in _sql_literals("_coverage_summary") if s == "deals"]
    assert tables or re.search(r"FROM\s+deals\b", cov_sql), \
        "ma_transactions must count `deals`"


def test_deals_count_is_quarantine_guarded_and_percent_free():
    """`deals` is 4,711 raw / 1,843 live. Publishing the raw count to agents
    re-inflates the number #2062-era work brought back to canon.

    The predicate used to be a function-LOCAL literal here (#2071's
    `DEALS_OK = "..."` inside _coverage_summary), so this test scanned
    _coverage_summary's SQL literals for `quarantine_`. It is now imported
    from util/deals.py — the same consolidation util/capacity_pipeline.py
    got, and for the same reason: a function-local predicate cannot be
    imported, therefore cannot be checked, therefore drifts. So this now
    asserts the IMPORT and the module-level properties, and the census in
    tests/test_deals_guard.py owns "is every served read guarded".
    """
    from util.deals import DEALS_OK

    tree = _module_tree()
    imported = any(
        isinstance(n, ast.ImportFrom) and n.module == "util.deals"
        and any(a.name == "DEALS_OK" for a in n.names)
        for n in ast.walk(tree))
    assert imported, (
        "routes/agent_index.py no longer imports DEALS_OK from util.deals. If "
        "the predicate was re-inlined, that is the #2071 defect returning — "
        "see util/deals.py and tests/test_deals_guard.py.")

    # _coverage_summary must still PAIR the deals table with that guard.
    func = _func_node("_coverage_summary")
    hit = None
    for t in (n for n in ast.walk(func) if isinstance(n, ast.Tuple)):
        if t.elts and isinstance(t.elts[0], ast.Constant) \
                and t.elts[0].value == "deals":
            hit = ast.unparse(t)
            break
    assert hit is not None, (
        "_coverage_summary no longer pairs 'deals' with its per-table WHERE "
        "in a tuple — re-check the guard by hand and update this test")
    assert "DEALS_OK" in hit, f"the deals count is unguarded: {hit}"

    # deals carries a legitimate non-quarantine flag (cumulative_capex, 7 rows),
    # so the prefix test is required — the strict `= ''` form would drop them.
    assert "LEFT(data_flag" in DEALS_OK, (
        "use LEFT(), the canonical deals predicate (deals carries a legitimate "
        "non-quarantine flag, cumulative_capex, so the prefix test is required)")

    # A literal % alongside a params tuple makes psycopg2 attempt substitution
    # and 500s the route -- the documented trap in util/capacity_pipeline.
    assert "%" not in DEALS_OK, \
        f"literal % in the deals guard is a live 500: {DEALS_OK}"


def test_countries_come_from_discovered_facilities_not_facilities():
    """178 is canon; facilities yields 186, the legacy-186 class #1958/#1966 closed."""
    cov_sql = " ".join(" ".join(s.split()) for s in _sql_literals("_coverage_summary"))
    m = re.search(r"DISTINCT\s+country\s+FROM\s+(\w+)", cov_sql)
    assert m, "countries_covered query not found"
    assert m.group(1) == "discovered_facilities", (
        f"countries_covered reads `{m.group(1)}`. discovered_facilities is canon "
        "(178); facilities returns 186, the legacy count closed by #1958/#1966.")


def test_safe_fetchall_still_unpoisons_even_though_it_hides_the_error():
    """_safe_fetchall stays lossy by design, but must not leave a dead txn."""
    cur = _FakeCursor(fail_substrings=("SELECT 1",))
    rows = ai._safe_fetchall(cur, "SELECT 1")
    assert rows == []
    assert cur.connection.rollbacks > 0
    rows2 = ai._safe_fetchall(cur, "SELECT 2")
    assert rows2 == [(0,)], "the next query must survive the previous failure"


def test_try_fetchall_reports_the_error_text():
    cur = _FakeCursor(fail_substrings=("SELECT boom",))
    rows, err = ai._try_fetchall(cur, "SELECT boom")
    assert rows == []
    assert err and "does not exist" in err
    rows_ok, err_ok = ai._try_fetchall(cur, "SELECT ok")
    assert err_ok is None


# ------------------------------------------------------------- anti-vacuous

def test_fence_is_not_vacuous():
    """If _coverage_summary stops emitting counts, every assertion above passes
    trivially. Pin the shape so a refactor cannot silently gut this file."""
    out = ai._coverage_summary(_FakeCursor(rows=_LIVE_ROWS))
    numeric = [k for k in _COUNT_KEYS if isinstance(out.get(k), int)]
    assert len(numeric) >= 7, (
        f"only {len(numeric)} counts produced; this fence assumes >= 7 "
        "(facilities, discovered_facilities, pipeline, dcpi, news, deals, listings)")
    assert isinstance(out.get("countries_covered"), list)
    assert "complete" in out
