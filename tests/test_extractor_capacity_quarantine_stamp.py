"""The capacity writer stamps the quarantine taxonomy at insert time.

Classification used to happen only when a human ran
repair_capacity_pipeline_quarantine.py --apply, so the served SUM
(COALESCE(data_flag,'')='') drifted upward between repair runs: stamps
frozen 2026-07-31 → by 2026-08-17 the served SUM read 791.94 GW against
550.08 defensible (+241.86 GW), almost entirely 19 rows ≥ 5,000 MW —
utility interconnection-queue announcements (Dominion 53,800 MW) extracted
as one building each — plus operator-Unknown rows. insert_capacity now
stamps every row it keeps, in the insert transaction, with the same arms
and precedence the repair applies. These guards pin what makes that real:

  1. ONE STATEMENT OF THE RULES — the arms live in util.capacity_pipeline
     and BOTH consumers (the writer's stamp UPDATE, the repair's
     CLASSIFY_SQL) derive from that import. A hand-copy is how a writer
     and its auditor drift apart; drift is the defect this PR exists for.
  2. THE ARMS THEMSELVES ARE PINNED — threshold, status set, operator
     sentinels, first-match precedence. These are the rules the 2026-08-17
     repair applied with human sign-off; changing them is a decision, not
     a refactor.
  3. %-FREE — the writer interpolates the arms into a parameterized
     UPDATE; a literal % (the original ILIKE '%unknown%') makes psycopg2
     attempt substitution and error every insert.
  4. IT RUNS, IN THE RIGHT PLACE — the real insert_capacity, executed
     against stub cursors, must stamp the kept row by its RETURNING id,
     after the twin probe, and never stamp a withdrawn or conflicted row.

All static/AST + stub-executed — CI runs with no DATABASE_URL. Every helper
asserts it FOUND its target first: an empty parse satisfies every "not in".
"""

import ast
import os
import sys

SRC = os.path.join(os.path.dirname(__file__), "..", "extractor_cron.py")
REPAIR = os.path.join(os.path.dirname(__file__), "..",
                      "repair_capacity_pipeline_quarantine.py")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _tree(path=SRC):
    with open(path, encoding="utf-8") as f:
        return ast.parse(f.read())


def _func(tree, name):
    fns = [n for n in ast.walk(tree)
           if isinstance(n, ast.FunctionDef) and n.name == name]
    assert fns, f"{name} not found"
    return fns[0]


def _assign(tree, name, where):
    hits = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == name
                    for t in n.targets)]
    assert hits, f"no assignment to {name} in {where}"
    return hits[0]


def _strings(node):
    return [n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _calls(node, name):
    return [n for n in ast.walk(node) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name) and n.func.id == name]


# ── 1 · one statement of the rules, both consumers derive from it ────

def test_writer_imports_the_arms_and_derives_the_stamp_from_them():
    tree = _tree()
    imports = {(n.module, a.name) for n in ast.walk(tree)
               if isinstance(n, ast.ImportFrom) and n.module
               for a in n.names}
    assert ("util.capacity_pipeline", "cp_classify_arms") in imports, (
        "extractor_cron no longer imports cp_classify_arms from "
        "util.capacity_pipeline — a private copy of the taxonomy drifts "
        "from the repair script the moment either changes")
    stamp = _assign(tree, "_CAP_STAMP_SQL", SRC)
    assert _calls(stamp.value, "cp_classify_arms"), (
        "_CAP_STAMP_SQL no longer derives from cp_classify_arms() — the "
        "writer and the repair are two statements of one rule set again")
    offenders = [s for s in _strings(stamp.value)
                 if "quarantine_" in s or "5000" in s or "operational" in s
                 or "Unknown" in s or "strpos" in s]
    assert not offenders, (
        "_CAP_STAMP_SQL restates classification content instead of "
        f"deriving it from cp_classify_arms: {offenders[:1]}")


def test_repair_script_derives_its_case_from_the_same_arms():
    tree = _tree(REPAIR)
    imports = {(n.module, a.name) for n in ast.walk(tree)
               if isinstance(n, ast.ImportFrom) and n.module
               for a in n.names}
    assert ("util.capacity_pipeline", "cp_classify_arms") in imports, (
        "the repair script no longer imports cp_classify_arms — its "
        "CLASSIFY_SQL and the writer's stamp can drift apart")
    classify = _assign(tree, "CLASSIFY_SQL", REPAIR)
    assert _calls(classify.value, "cp_classify_arms"), (
        "CLASSIFY_SQL no longer interpolates cp_classify_arms() — the "
        "repair restated (or dropped) the taxonomy the writer stamps")
    # The dup CTE legitimately names its own partition columns; what may
    # NOT reappear locally is the content of arms 1–3.
    offenders = [s for s in _strings(classify.value)
                 if "quarantine_aggregate" in s
                 or "quarantine_not_pipeline" in s
                 or "quarantine_unparsed" in s
                 or "5000" in s or "ILIKE" in s]
    assert not offenders, (
        "CLASSIFY_SQL restates arms 1–3 instead of deriving them: "
        f"{offenders[:1]}")
    assert any("quarantine_duplicate" in s
               for s in _strings(classify.value)), (
        "CLASSIFY_SQL lost its local duplicate arm — the repair is the "
        "only pass that can number duplicates across the whole table")


# ── 2 · the arms themselves — the humanly-approved rule set ──────────

def test_arm_content_and_precedence_are_the_approved_rule_set():
    from util.capacity_pipeline import cp_classify_arms
    arms = cp_classify_arms()
    assert arms.count("WHEN ") == 3, (
        "cp_classify_arms no longer carries exactly the three insert-time "
        f"arms (duplicate handling belongs to the writer probe): {arms}")
    a = arms.index("'quarantine_aggregate'")
    n = arms.index("'quarantine_not_pipeline'")
    u = arms.index("'quarantine_unparsed'")
    assert a < n < u, (
        "arm precedence changed — first match wins, so ordering IS the "
        "classification: a ≥5,000 MW row with status 'operational' must "
        f"stamp aggregate, not not_pipeline: {arms}")
    assert "capacity_mw >= 5000" in arms, (
        "the aggregate threshold moved off 5,000 MW — that value is the "
        "2026-08-17 human-approved rule; changing it is a decision")
    assert "('operational','acquisition','cancelled','lease')" in arms, (
        "the not-pipeline status set changed from the approved four")
    assert "lower(COALESCE(status,''))" in arms, (
        "status matching is no longer case-insensitive over NULL-safe "
        "input — 'Operational' rows would sail through")
    assert "IN ('','Unknown','None')" in arms, (
        "the unparsed-operator sentinels changed from the approved set")
    assert "strpos(lower(COALESCE(operator,'')), 'unknown') > 0" in arms, (
        "the containment probe for 'unknown' operators changed — it must "
        "stay the %-free equivalent of the original ILIKE '%unknown%'")


def test_alias_form_qualifies_every_column():
    from util.capacity_pipeline import cp_classify_arms
    aliased = cp_classify_arms("c")
    for col in ("capacity_mw", "status", "operator"):
        assert f"c.{col}" in aliased, (
            f"cp_classify_arms('c') leaves {col} unqualified — inside the "
            "repair's join, Postgres resolves it ambiguously or errors")
    bare = cp_classify_arms()
    assert "c." not in bare, "bare form leaked an alias prefix"


def test_arms_are_percent_free_in_both_forms():
    from util.capacity_pipeline import cp_classify_arms
    for form in (cp_classify_arms(), cp_classify_arms("c")):
        assert "%" not in form, (
            "cp_classify_arms carries a literal % — the writer "
            "interpolates it into a parameterized UPDATE, so psycopg2 "
            "would attempt substitution on it and error every capacity "
            f"insert: {form}")


# ── 3 · the real writer stamps, in the right place ───────────────────

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

_SIGNALS = {"operator": "Dominion", "capacity_mw": 53800.0, "market": None,
            "status": "announced", "confidence": 0.9, "source": "regex"}


def _load_insert_capacity():
    fn = _func(_tree(), "insert_capacity")
    ns = {"_CAP_TWIN_SQL": _PROBE, "_CAP_STAMP_SQL": _STAMP}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), SRC, "exec"), ns)
    return ns["insert_capacity"]


def test_kept_row_is_stamped_by_its_returned_id_after_the_probe():
    insert_capacity = _load_insert_capacity()
    cur = _Cursor([(77,), None])            # INSERT landed; no twin
    assert insert_capacity(_Conn(cur), dict(_SIGNALS), "ann-1") is True, (
        "a kept-and-stamped row is still an inserted row — "
        "extractor_runs.capacity_inserted must count it")
    assert len(cur.executed) == 3 and cur.executed[2][0] == _STAMP, (
        "insert_capacity did not stamp the kept row after the twin probe: "
        f"{[str(s)[:60] for s, _ in cur.executed]}")
    assert cur.executed[2][1] == (77,), (
        "the stamp must anchor on the FRESH row's id (RETURNING id) — "
        f"anything else classifies someone else's row: {cur.executed[2][1]!r}")


def test_withdrawn_twin_is_never_stamped():
    insert_capacity = _load_insert_capacity()
    cur = _Cursor([(77,), (1,)])            # INSERT landed; a twin answered
    assert insert_capacity(_Conn(cur), dict(_SIGNALS), "ann-2") is False
    assert not any(s == _STAMP for s, _ in cur.executed), (
        "insert_capacity stamped a row it then withdrew — the stamp must "
        "run only on the kept path")


def test_conflicted_insert_is_never_stamped():
    insert_capacity = _load_insert_capacity()
    cur = _Cursor([None])                   # ON CONFLICT ... DO NOTHING fired
    assert insert_capacity(_Conn(cur), dict(_SIGNALS), "ann-3") is False
    assert len(cur.executed) == 1, (
        "when the same-article ON CONFLICT no-ops the INSERT, nothing else "
        f"may run: {[str(s)[:60] for s, _ in cur.executed]}")


def test_built_stamp_sql_is_one_parameterized_update_by_id():
    from util.capacity_pipeline import cp_classify_arms
    stamp = _assign(_tree(), "_CAP_STAMP_SQL", SRC)
    ns = {"cp_classify_arms": cp_classify_arms}
    exec(compile(ast.Module(body=[stamp], type_ignores=[]), SRC, "exec"), ns)
    sql = ns["_CAP_STAMP_SQL"]
    assert sql.startswith("UPDATE capacity_pipeline SET data_flag = CASE "), (
        f"the stamp is no longer an UPDATE of data_flag: {sql[:80]}")
    assert sql.endswith(" ELSE NULL END WHERE id = %s"), (
        "the stamp lost its id predicate or its explicit clean arm — an "
        f"unscoped UPDATE would reclassify the whole table per insert: "
        f"{sql[-80:]}")
    assert sql.count("%") == 1, (
        "the stamp carries a % beyond its single id placeholder — "
        "psycopg2 will attempt substitution on it and error the insert")
    assert cp_classify_arms() in sql, (
        "the built stamp no longer contains the canonical arms verbatim")
