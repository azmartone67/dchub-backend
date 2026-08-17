"""The deal writer withdraws byte-identical twins (Shell #36 lane 3a).

extractor_cron mints deal ids as ext_<md5(announcement_id)[:20]>: the hash is
over the ANNOUNCEMENT, so one deal announced in N articles arrives as N
distinct ids AND N distinct source_announcement_ids — neither ON CONFLICT
target in insert_deal can fire, and 107 of 2,144 served rows (5.0%) were such
byte-copies on 2026-08-16 (lane 3a red, same defect shape as the retired
AUTO-<date> ids that lane 3b watches for). repair_deals_exact_dupes.py
quarantined the stock; the writer-side twin probe stops the regrowth. These
guards pin the properties that make the probe real:

  1. IMPORTED, NOT RESTATED — the group key and served predicate come from
     the modules that own them (graph_spine_master_shell._DEAL_BUSINESS_COLS,
     util.deals.deals_ok). A hand-copy is how the probe and lane 3a drift
     apart, which is the exact defect that produced the AUTO- era dupes.
  2. NULL-SAFE — the tuple comparison is IS NOT DISTINCT FROM. Lane 3a's
     GROUP BY treats NULLs as equal, and most of the 19 columns are NULL on
     every extractor row, so `=` would match nothing and the probe would be
     a silent no-op that still returns rows-inserted counts that look right.
  3. IT RUNS AND IT WITHDRAWS — the real insert_deal, executed against stub
     cursors, must probe after the INSERT, DELETE the fresh row when a twin
     answers, report False so the run log stays honest, and keep the
     same-article ON CONFLICT short-circuit it always had.

All static/AST + stub-executed — CI runs with no DATABASE_URL. Every helper
asserts it FOUND its target first: an empty parse satisfies every "not in".
"""

import ast
import hashlib
import os

SRC = os.path.join(os.path.dirname(__file__), "..", "extractor_cron.py")


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
    assert ("routes.graph_spine_master_shell", "_DEAL_BUSINESS_COLS") in imports, (
        "insert_deal's twin probe no longer imports the 19-column group key "
        "from the shell that audits it — a private copy drifts the moment "
        "lane 3a's definition moves")
    assert ("util.deals", "deals_ok") in imports, (
        "the twin probe no longer imports the served predicate from "
        "util.deals — the census in tests/test_deals_guard.py cannot see "
        "root-level crons, so the import IS the guard here")

    # The probe must actually DERIVE from those imports, not just carry them.
    cols = _assign(tree, "_DEAL_COLS")
    assert any(isinstance(n, ast.Name) and n.id == "_DEAL_BUSINESS_COLS"
               for n in ast.walk(cols.value)), (
        "_DEAL_COLS is no longer split from the imported _DEAL_BUSINESS_COLS")
    twin = _assign(tree, "_TWIN_SQL")
    assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and n.func.id == "deals_ok" for n in ast.walk(twin.value)), (
        "_TWIN_SQL no longer calls deals_ok() — quarantined rows would block "
        "inserts, or (predicate dropped entirely) the probe stops mirroring "
        "the lane it exists to keep green")

    # A restated copy of the key would carry this distinctive column name as
    # a string literal somewhere in the module; the only place it may live is
    # routes/graph_spine_master_shell.py, where the import points.
    offenders = [s for s in _strings(tree) if "deal_category" in s]
    assert not offenders, (
        "extractor_cron.py restates the deal business-column list instead of "
        f"importing it: {offenders[:1]}")


# ── 2 · NULL-safe comparison ─────────────────────────────────────────

def test_twin_probe_compares_null_safe_over_the_live_table():
    twin = _assign(_tree(), "_TWIN_SQL")
    text = " ".join(_strings(twin.value))
    assert "FROM deals" in text, "_TWIN_SQL no longer reads the deals table"
    assert "IS NOT DISTINCT FROM" in text, (
        "_TWIN_SQL compares the 19-column tuples with something other than "
        "IS NOT DISTINCT FROM. GROUP BY (lane 3a's key) treats NULLs as "
        "equal and most business columns are NULL on every extractor row — "
        "an `=` comparison matches nothing and the probe becomes a silent "
        "no-op while dupes regrow")


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

_SIGNALS = {"operator": "CoreWeave", "deal_type": "acquisition",
            "deal_value_usd": 5_000_000_000, "market": "Dallas",
            "confidence": 0.65, "source": "regex"}


def _load_insert_deal():
    fn = _func(_tree(), "insert_deal")
    ns = {"hashlib": hashlib, "_TWIN_SQL": _PROBE}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), SRC, "exec"), ns)
    return ns["insert_deal"]


def test_a_served_twin_withdraws_the_fresh_row_and_reports_false():
    insert_deal = _load_insert_deal()
    deal_id = "ext_" + hashlib.md5(b"ann-1").hexdigest()[:20]
    cur = _Cursor([(deal_id,), (1,)])       # INSERT landed; a twin answered
    assert insert_deal(_Conn(cur), dict(_SIGNALS), "ann-1") is False, (
        "a withdrawn duplicate must not count as an inserted deal — "
        "extractor_runs.deals_inserted would over-report")
    assert len(cur.executed) == 3 and cur.executed[1][0] == _PROBE, (
        "insert_deal did not run the twin probe after the INSERT: "
        f"{[s[:60] for s, _ in cur.executed]}")
    sql, params = cur.executed[2]
    assert "DELETE FROM deals" in sql and params == (deal_id,), (
        "the fresh duplicate row was not withdrawn — lane 3a regrows with "
        f"the next news cycle: {(sql[:80], params)}")


def test_no_twin_keeps_the_row_and_reports_true():
    insert_deal = _load_insert_deal()
    cur = _Cursor([("x",), None])           # INSERT landed; no twin
    assert insert_deal(_Conn(cur), dict(_SIGNALS), "ann-2") is True
    assert cur.executed[1][0] == _PROBE, "the probe must run on every insert"
    assert not any("DELETE" in s.upper() for s, _ in cur.executed), (
        "insert_deal deleted a row that has no served twin")


def test_same_article_conflict_still_short_circuits():
    insert_deal = _load_insert_deal()
    cur = _Cursor([None])                   # ON CONFLICT ... DO NOTHING fired
    assert insert_deal(_Conn(cur), dict(_SIGNALS), "ann-3") is False
    assert len(cur.executed) == 1, (
        "when the same-article ON CONFLICT no-ops the INSERT, nothing else "
        f"may run: {[s[:60] for s, _ in cur.executed]}")


def test_insert_keeps_the_same_article_conflict_target():
    fn = _func(_tree(), "insert_deal")
    sqls = [s for s in _strings(fn) if "INSERT INTO deals" in s]
    assert sqls, "insert_deal lost its INSERT INTO deals statement"
    assert any("ON CONFLICT (source_announcement_id)" in " ".join(s.split())
               for s in sqls), (
        "the twin probe replaced (rather than composed with) the "
        "same-article ON CONFLICT guard — re-processing one announcement "
        "would double-insert again")
