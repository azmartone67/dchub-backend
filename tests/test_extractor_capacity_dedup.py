"""The capacity writer withdraws cross-article twins (deals' #2776, one table over).

extractor_cron's insert_capacity conflicts only on source_announcement_id:
one project covered by N articles arrives as N distinct ids, so the target
never fires across articles — and the table's own UNIQUE (operator, market,
phase, capacity_mw) never fires either, because btree UNIQUE treats NULLs as
distinct and the writer never sets phase. Measured live 2026-08-16: 12 of
1,338 served rows were such copies, double-counting 47.3 GW of the 791.9 GW
served SUM (6.0%) — OpenAI/Ohio/10,000 MW landed four times. The writer-side
twin probe (mirror of insert_deal's, PR #2776) stops the regrowth. These
guards pin the properties that make the probe real:

  1. IMPORTED, NOT RESTATED — the group key and served predicate come from
     util.capacity_pipeline (CP_BUSINESS_COLS, cp_ok), the module that owns
     capacity_pipeline's served semantics. A hand-copy is how a probe and
     its auditor drift apart; drift is the defect class this exists for.
  2. THE KEY IS THE 4-COLUMN BUSINESS IDENTITY — not the full stored tuple.
     extraction_confidence wobbles between articles about one project and
     hid 6 of the measured 12 from a byte-identical comparison; status stays
     IN the key so announced → under_construction remains a real new row.
  3. NULL-SAFE — the tuple comparison is IS NOT DISTINCT FROM. market is
     NULL on real extractor rows (the measured Meta group), so `=` would
     silently no-op the probe exactly where it is needed.
  4. IT RUNS AND IT WITHDRAWS — the real insert_capacity, executed against
     stub cursors, must probe after the INSERT, DELETE the fresh row when a
     served twin answers, report False so extractor_runs stays honest, and
     keep the same-article ON CONFLICT short-circuit it always had.

All static/AST + stub-executed — CI runs with no DATABASE_URL. Every helper
asserts it FOUND its target first: an empty parse satisfies every "not in".
"""

import ast
import os
import sys

SRC = os.path.join(os.path.dirname(__file__), "..", "extractor_cron.py")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _tree():
    with open(SRC, encoding="utf-8") as f:
        return ast.parse(f.read())


def _func(tree, name):
    fns = [n for n in ast.walk(tree)
           if isinstance(n, ast.FunctionDef) and n.name == name]
    assert fns, f"{name} not found in {SRC}"
    return fns[0]


def _assign(tree, name):
    hits = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == name
                    for t in n.targets)]
    assert hits, f"no assignment to {name} in {SRC}"
    return hits[0]


def _strings(node):
    return [n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


# ── 1 · imported, not restated ───────────────────────────────────────

def test_twin_key_and_predicate_are_imported_not_restated():
    tree = _tree()
    imports = {(n.module, a.name) for n in ast.walk(tree)
               if isinstance(n, ast.ImportFrom) and n.module
               for a in n.names}
    assert ("util.capacity_pipeline", "CP_BUSINESS_COLS") in imports, (
        "insert_capacity's twin probe no longer imports the business-column "
        "key from util.capacity_pipeline — a private copy drifts the moment "
        "the canonical definition moves")
    assert ("util.capacity_pipeline", "cp_ok") in imports, (
        "the twin probe no longer imports the served predicate from "
        "util.capacity_pipeline — the census in "
        "tests/test_capacity_pipeline_guard.py only sweeps served surfaces, "
        "not root-level crons, so the import IS the guard here")

    # The probe must actually DERIVE from those imports, not just carry them.
    cols = _assign(tree, "_CAP_COLS")
    assert any(isinstance(n, ast.Name) and n.id == "CP_BUSINESS_COLS"
               for n in ast.walk(cols.value)), (
        "_CAP_COLS is no longer split from the imported CP_BUSINESS_COLS")
    twin = _assign(tree, "_CAP_TWIN_SQL")
    assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and n.func.id == "cp_ok" for n in ast.walk(twin.value)), (
        "_CAP_TWIN_SQL no longer calls cp_ok() — quarantined rows would "
        "block fresh inserts, or (predicate dropped entirely) the probe "
        "stops matching what the served surfaces actually serve")

    # Restated column names inside the probe would appear as string literals
    # naming business columns; in the legitimate build every column name
    # flows from the derived _CAP_COLS list, so no constant in the twin-SQL
    # expression may carry one. (Scoped to the assign: the INSERT statement
    # legitimately names its own column list.)
    offenders = [s for s in _strings(twin.value)
                 if "capacity_mw" in s or "operator" in s]
    assert not offenders, (
        "_CAP_TWIN_SQL restates business column names instead of deriving "
        f"them from CP_BUSINESS_COLS: {offenders[:1]}")


def test_canonical_key_is_the_four_column_business_identity():
    from util.capacity_pipeline import CP_BUSINESS_COLS
    assert [c.strip() for c in CP_BUSINESS_COLS.split(",")] == [
        "operator", "capacity_mw", "market", "status"], (
        "CP_BUSINESS_COLS changed. status must stay IN (a progression is a "
        "new row, not a dupe); extraction_confidence must stay OUT (it "
        "varies across articles about one project and hid 6 of the measured "
        "12 duplicates from a full-tuple comparison). If the identity "
        "genuinely evolved, update this pin and the measurement in "
        "util/capacity_pipeline.py together.")
    assert "%" not in CP_BUSINESS_COLS, (
        "CP_BUSINESS_COLS carries a literal % — callers interpolate it into "
        "queries that also pass a params tuple; psycopg2 would attempt "
        "substitution and 500 the caller")


# ── 2 · NULL-safe comparison ─────────────────────────────────────────

def test_twin_probe_compares_null_safe_over_the_live_table():
    twin = _assign(_tree(), "_CAP_TWIN_SQL")
    text = " ".join(_strings(twin.value))
    assert "FROM capacity_pipeline" in text, (
        "_CAP_TWIN_SQL no longer reads the capacity_pipeline table")
    assert "IS NOT DISTINCT FROM" in text, (
        "_CAP_TWIN_SQL compares the business tuples with something other "
        "than IS NOT DISTINCT FROM. market is NULL on real extractor rows "
        "(the measured Meta 1,000 MW group) — an `=` comparison matches "
        "nothing and the probe becomes a silent no-op while dupes regrow")


# ── 3 · the real function probes, withdraws, reports ─────────────────

class _Cursor:
    def __init__(self, fetches):
        self.fetches = list(fetches)
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self.fetches.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


_PROBE = "<twin-probe>"
_STAMP = "<quarantine-stamp>"

_SIGNALS = {"operator": "CoreWeave", "capacity_mw": 250.0, "market": "Dallas",
            "status": "announced", "confidence": 0.85, "source": "regex"}


def _load_insert_capacity():
    fn = _func(_tree(), "insert_capacity")
    ns = {"_CAP_TWIN_SQL": _PROBE, "_CAP_STAMP_SQL": _STAMP}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), SRC, "exec"), ns)
    return ns["insert_capacity"]


def test_a_served_twin_withdraws_the_fresh_row_and_reports_false():
    insert_capacity = _load_insert_capacity()
    cur = _Cursor([(77,), (1,)])            # INSERT landed; a twin answered
    assert insert_capacity(_Conn(cur), dict(_SIGNALS), "ann-1") is False, (
        "a withdrawn duplicate must not count as an inserted row — "
        "extractor_runs.capacity_inserted would over-report")
    assert len(cur.executed) == 3 and cur.executed[1][0] == _PROBE, (
        "insert_capacity did not run the twin probe after the INSERT: "
        f"{[str(s)[:60] for s, _ in cur.executed]}")
    assert cur.executed[1][1] == (77,), (
        "the probe must anchor on the FRESH row's id (RETURNING id), not on "
        f"anything else: {cur.executed[1][1]!r}")
    sql, params = cur.executed[2]
    assert "DELETE FROM capacity_pipeline" in sql and params == (77,), (
        "the fresh duplicate row was not withdrawn — the served SUM "
        f"double-counts with the next news cycle: {(str(sql)[:80], params)}")


def test_no_twin_keeps_the_row_and_reports_true():
    insert_capacity = _load_insert_capacity()
    cur = _Cursor([(77,), None])            # INSERT landed; no twin
    assert insert_capacity(_Conn(cur), dict(_SIGNALS), "ann-2") is True
    assert cur.executed[1][0] == _PROBE, "the probe must run on every insert"
    assert not any("DELETE" in str(s).upper() for s, _ in cur.executed), (
        "insert_capacity deleted a row that has no served twin")


def test_same_article_conflict_still_short_circuits():
    insert_capacity = _load_insert_capacity()
    cur = _Cursor([None])                   # ON CONFLICT ... DO NOTHING fired
    assert insert_capacity(_Conn(cur), dict(_SIGNALS), "ann-3") is False
    assert len(cur.executed) == 1, (
        "when the same-article ON CONFLICT no-ops the INSERT, nothing else "
        f"may run: {[str(s)[:60] for s, _ in cur.executed]}")


def test_insert_keeps_the_same_article_conflict_target():
    fn = _func(_tree(), "insert_capacity")
    sqls = [s for s in _strings(fn) if "INSERT INTO capacity_pipeline" in s]
    assert sqls, "insert_capacity lost its INSERT INTO capacity_pipeline"
    assert any("ON CONFLICT (source_announcement_id)" in " ".join(s.split())
               for s in sqls), (
        "the twin probe replaced (rather than composed with) the "
        "same-article ON CONFLICT guard — re-processing one announcement "
        "would double-insert again")
