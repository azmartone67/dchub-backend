"""The resolver's blind-spot measurement must stay INDEXABLE and FAIL CLOSED.

2026-08-23. Two master shells — #36 lane 5a (es_blindspot, critical=True) and
brain-autonomy's news_entity_reresolve trigger — each RESTATED this query, and
both copies stated it in a form the planner cannot index:

    lower(f.name) LIKE lower(trim(e.entity_name)) || ' ' || chr(37)

The pattern is computed from the OUTER row, so it can only ever be a Join
Filter: 1,903 x 22,162 rows, cost 380,622, and it hit the 15s
statement_timeout EVERY time. Both callers swallowed the error and reported
UNMEASURED, so the actuator could never fire and /admin/brain-autonomy took
~17s at the origin — a 502 through the CF edge. Measured on prod:
15,060ms -> 55ms once the predicate became a byte RANGE the existing
idx_facilities_name_lower btree can serve.

These guards are BEHAVIOURAL where they can be (a stub cursor — CI has no
DATABASE_URL, but the helper takes a cursor, so its logic is testable without
one) and static only for the "nobody restated it again" property.
"""

import ast
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")
HELPER_SRC = os.path.join(ROOT, "routes", "news_entity_extraction.py")
SHELLS = {
    "brain_autonomy_master_shell.py": os.path.join(
        ROOT, "routes", "brain_autonomy_master_shell.py"),
    "graph_spine_master_shell.py": os.path.join(
        ROOT, "routes", "graph_spine_master_shell.py"),
}

# The shape that could not be indexed. Written split so this guard's own
# source does not trip the grep it performs.
SLOW_FORM = "LIKE" + " lower(trim(e.entity_name))"


def _mod():
    import routes.news_entity_extraction as m
    m._COLLATION_BYTE_ORDERED = None      # cache is per-process; reset per test
    return m


class _Cur:
    """Minimal cursor stand-in. Records every SQL it is handed."""

    def __init__(self, collate="C.UTF-8", count=(7,), raise_on_count=False):
        self._collate, self._count = collate, count
        self._raise_on_count = raise_on_count
        self.executed, self.rolled_back = [], False
        self._last = None
        outer = self

        class _Conn:
            def rollback(self):
                outer.rolled_back = True
        self.connection = _Conn()

    def execute(self, sql, args=None):
        self.executed.append(sql)
        if "pg_database" in sql:
            self._last = None if self._collate is None else (self._collate,)
            return
        if self._raise_on_count:
            raise RuntimeError("canceling statement due to statement timeout")
        self._last = self._count

    def fetchone(self):
        return self._last

    def count_sql(self):
        return [s for s in self.executed if "news_discovered_entities" in s]


# ── 1. fail closed: a database that is not byte-ordered yields no number ──

def test_non_byte_ordered_collation_returns_none_and_never_counts():
    m = _mod()
    cur = _Cur(collate="en_US.UTF-8")
    assert m.entity_blindspot_count(cur) is None, (
        "a linguistic collation makes the range predicate UNDER-include; "
        "es_blindspot is critical=True, so a silently low number would read "
        "as 'no blind spot' and go green. It must refuse to answer.")
    assert cur.count_sql() == [], (
        "it must not even run the count once the collation fails the witness")


def test_unwitnessable_collation_returns_none():
    m = _mod()
    assert m.entity_blindspot_count(_Cur(collate=None)) is None, (
        "a database that would not report its collation is UNMEASURED")


def test_query_failure_returns_none_not_zero():
    m = _mod()
    cur = _Cur(raise_on_count=True)
    assert m.entity_blindspot_count(cur) is None, (
        "a failed count must be None (UNMEASURED), never 0 — 0 means "
        "'no blind spot' and is a green light to stop looking")
    assert cur.rolled_back, "a failed query must roll back its aborted txn"


# ── 2. the happy path really measures, and states an INDEXABLE predicate ──

def test_byte_ordered_collation_measures_and_returns_the_count():
    m = _mod()
    assert m.entity_blindspot_count(_Cur(collate="C.UTF-8", count=(7,))) == 7


def test_the_count_predicate_is_a_range_the_btree_can_serve():
    m = _mod()
    cur = _Cur()
    m.entity_blindspot_count(cur)
    sql = " ".join(cur.count_sql())
    assert sql, "no count query was issued"
    assert "lower(f.name) >=" in sql and "lower(f.name) <" in sql, (
        "the half-open byte RANGE is what idx_facilities_name_lower serves "
        "as an Index Cond; without it the planner falls back to a Join "
        "Filter over every facility and the 15s timeout kills it")
    assert "|| ' '" in sql and "|| '!'" in sql, (
        "the bounds must be <entity>' ' .. <entity>'!' — chr(32)/chr(33) are "
        "adjacent, which is exactly what makes the range hold every string "
        "starting with '<entity> ' and nothing else")
    assert "LIKE" in sql, (
        "the original LIKE is KEPT as a recheck: it is the semantic "
        "definition of the match and it is free (55ms with, 63ms without) "
        "on the few rows the index returns")


def test_the_stoplist_is_applied_from_the_resolver_itself():
    m = _mod()
    cur = _Cur()
    m.entity_blindspot_count(cur)
    sql = " ".join(cur.count_sql())
    assert "NOT IN" in sql, "the resolver's stoplist must still be applied"
    for word in list(m._GENERIC_PREFIX_STOP)[:3]:
        assert "'" + word + "'" in sql, (
            f"stop word {word!r} missing — the lane must mirror the "
            "resolver's guards or it measures an unreachable target")


# ── 3. nobody restates it again — that is what rotted it in two places ──

def test_neither_shell_restates_the_unindexable_query():
    for name, path in SHELLS.items():
        with open(path, encoding="utf-8") as f:
            src = f.read()
        assert SLOW_FORM not in src, (
            f"{name} restates the un-indexable LIKE-prefix form. That query "
            "cannot use an index and times out at 15s. Call "
            "news_entity_extraction.entity_blindspot_count() instead.")


def test_both_shells_import_the_resident_measurement():
    for name, path in SHELLS.items():
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        imported = any(
            isinstance(n, ast.ImportFrom)
            and n.module == "routes.news_entity_extraction"
            and any(a.name == "entity_blindspot_count" for a in n.names)
            for n in ast.walk(tree))
        assert imported, (
            f"{name} must import entity_blindspot_count — the query is "
            "resident in one place so the two can never drift apart")


# ── 4. cursor OR connection — the two shells hand it different objects ────
# This is not hypothetical: the first cut of this refactor passed graph-spine's
# CONNECTION to a function that called cur.execute(). It raised inside the
# fail-soft except and returned a silent None — forever, and green-looking.
# Verified against the live database before this guard was written:
#   connection -> None   cursor -> 0

class _Conn:
    """Connection stand-in: has .cursor(), has no .execute() — exactly how
    psycopg2 tells the two apart."""

    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        outer = self._cur

        class _Ctx:
            def __enter__(self):
                return outer

            def __exit__(self, *a):
                return False
        return _Ctx()


def test_a_connection_measures_exactly_like_a_cursor():
    m = _mod()
    cur = _Cur(count=(7,))
    assert m.entity_blindspot_count(cur) == 7
    m._COLLATION_BYTE_ORDERED = None
    conn_cur = _Cur(count=(7,))
    assert m.entity_blindspot_count(_Conn(conn_cur)) == 7, (
        "graph-spine's _row() takes a CONNECTION and brain-autonomy's takes a "
        "CURSOR. Handing this the wrong one used to raise inside the fail-soft "
        "except and return a silent None — green-looking, forever.")
    assert conn_cur.count_sql(), "the connection path never ran the count"


def test_each_shell_passes_what_its_own_row_helper_takes():
    """The call sites, checked against each shell's OWN convention — the
    object type is exactly what broke, so read it from the AST, not a slice."""
    expected = {"brain_autonomy_master_shell.py": "cursor",
                "graph_spine_master_shell.py": "connection"}
    for name, path in SHELLS.items():
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        row = [n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_row"]
        assert row, f"{name}: _row() not found"
        opens_own_cursor = any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "cursor" for n in ast.walk(row[0]))
        takes = "connection" if opens_own_cursor else "cursor"
        assert takes == expected[name], (
            f"{name}: _row() now takes a {takes}, not a {expected[name]}. "
            "The entity_blindspot_count() call site was written for that "
            "convention; passing the other object returns a silent None.")
        called = any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "entity_blindspot_count" for n in ast.walk(tree))
        assert called, f"{name}: no entity_blindspot_count(...) call found"
