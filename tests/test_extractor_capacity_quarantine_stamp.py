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

★ EVERY LIVE WRITER, NOT JUST THE EXTRACTOR (2026-08-17, second pass).
extractor_cron was one of five writers into capacity_pipeline. Section 5
below extends these guards to the other two that actually run — the
facility→pipeline sync in crawler_scheduler.py (both SCHEDULE slots) and
extract_capacity_from_news in autonomous_brain.py — and section 6 makes
coverage self-policing: any NEW `INSERT INTO capacity_pipeline` anywhere in
the repo fails the build until it either classifies or is explicitly
recorded as dead with the measurement that showed it dead.

All static/AST + stub-executed — CI runs with no DATABASE_URL. Every helper
asserts it FOUND its target first: an empty parse satisfies every "not in".
"""

import ast
import os
import sys

SRC = os.path.join(os.path.dirname(__file__), "..", "extractor_cron.py")
REPAIR = os.path.join(os.path.dirname(__file__), "..",
                      "repair_capacity_pipeline_quarantine.py")
CRAWLER = os.path.join(os.path.dirname(__file__), "..",
                       "crawler_scheduler.py")
BRAIN = os.path.join(os.path.dirname(__file__), "..", "autonomous_brain.py")
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


# ── 4 · the OTHER live writers stamp too ────────────────────────────
#
# extractor_cron was one of five writers into capacity_pipeline. Measured
# against the Neon replica on 2026-08-17, two others actually run and were
# inserting unclassified rows:
#
#   crawler_scheduler.py  — the facility→pipeline sync, twice from SCHEDULE
#                           (_run_market_refresh 9/21 over the whole table,
#                           _run_facility_discovery 7/19 over 7 days). Its
#                           source set holds 2 rows ≥ 5,000 MW and 6 with an
#                           unknown provider, of 226 sync-eligible.
#   autonomous_brain.py   — extract_capacity_from_news; source
#                           'auto_extracted' is unique to it and covers 583
#                           rows, most recent 2026-08-14.
#
# Both build ONE statement that classifies in the INSERT itself. The shape
# is the same in both and it is the part worth pinning: the arms name
# capacity_pipeline's own columns (operator, capacity_mw, status), so the
# statement must first expose those names on the alias the arms are bound
# to. crawler_scheduler's source table calls them provider/power_mw and
# autonomous_brain's are bare %s parameters; in both cases aliasing the arms
# straight at the source would reference columns that do not exist and raise
# UndefinedColumn on every run — swallowed, in both callers, by a bare
# except that logs a warning and moves on.


def _built(path, name, extra=None):
    """exec ONE module-level assignment out of `path` and return its value.

    Executes the assignment alone — never the module — so no import side
    effect (crawler_scheduler starts threads at import) can reach CI.
    """
    tree = _tree(path)
    node = _assign(tree, name, path)
    ns = {"cp_classify_arms": cp_classify_arms_ref()}
    ns.update(extra or {})
    exec(compile(ast.Module(body=[node], type_ignores=[]), path, "exec"), ns)
    return ns[name]


def cp_classify_arms_ref():
    from util.capacity_pipeline import cp_classify_arms
    return cp_classify_arms


def _executed_names(fn):
    """Names passed as the first arg of any `.execute(...)` inside fn."""
    out = []
    for n in ast.walk(fn):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "execute" and n.args
                and isinstance(n.args[0], ast.Name)):
            out.append(n.args[0].id)
    return out


def _imports(tree):
    return {(n.module, a.name) for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) and n.module for a in n.names}


def _no_restatement(sql, where):
    offenders = [tok for tok in ("quarantine_aggregate", "quarantine_unparsed",
                                 "quarantine_not_pipeline", "5000", "strpos",
                                 "ILIKE", "'Unknown'", "operational")
                 if tok in sql.replace(cp_classify_arms_ref()("s"), "")]
    assert not offenders, (
        f"{where} restates classification content outside "
        f"cp_classify_arms(): {offenders}")


def test_crawler_sync_derives_both_variants_from_the_arms():
    tree = _tree(CRAWLER)
    assert ("util.capacity_pipeline", "cp_classify_arms") in _imports(tree), (
        "crawler_scheduler no longer imports cp_classify_arms — its "
        "facility→pipeline sync is a private copy of the taxonomy again")
    fn = _func(tree, "_cap_sync_sql")
    assert _calls(fn, "cp_classify_arms"), (
        "_cap_sync_sql stopped deriving its CASE from cp_classify_arms()")


def test_crawler_sync_sql_classifies_in_the_insert():
    from util.capacity_pipeline import cp_classify_arms
    sql = _built(CRAWLER, "_CAP_SYNC_ALL_SQL",
                 {"_cap_sync_sql": _load_cap_sync_sql()})
    assert sql.startswith("INSERT INTO capacity_pipeline ("), (
        f"the sync is no longer an INSERT into capacity_pipeline: {sql[:60]}")
    assert ", data_flag)" in sql.split(" SELECT ")[0], (
        "data_flag left the INSERT column list — the sync writes unclassified "
        f"rows again: {sql[:200]}")
    assert "CASE " + cp_classify_arms("s") + " ELSE NULL END" in sql, (
        "the sync's CASE is not the canonical arms with an explicit clean "
        "arm — a missing ELSE NULL leaves data_flag unset, which reads as "
        "clean anyway, so this must be explicit")
    _no_restatement(sql, "_CAP_SYNC_ALL_SQL")


def test_crawler_insert_and_select_lists_stay_the_same_length():
    """A column added to one list and not the other silently shifts values."""
    sql = _built(CRAWLER, "_CAP_SYNC_ALL_SQL",
                 {"_cap_sync_sql": _load_cap_sync_sql()})
    head, _, tail = sql.partition(" SELECT ")
    insert_cols = head[head.index("(") + 1:head.rindex(")")].split(",")
    select_items = tail[:tail.index(" FROM (")]
    # the trailing CASE is one item; count the leading s.<col> refs plus it
    s_refs = [t for t in select_items.split(",") if t.strip().startswith("s.")]
    assert len(insert_cols) == len(s_refs) + 1, (
        f"INSERT names {len(insert_cols)} columns but SELECT supplies "
        f"{len(s_refs) + 1} — values land in the wrong columns")
    assert insert_cols[-1].strip() == "data_flag", (
        "data_flag is no longer the LAST insert column, so it no longer "
        f"lines up with the trailing CASE: {insert_cols[-1]!r}")


def test_crawler_derived_table_exposes_the_columns_the_arms_name():
    """The load-bearing bit: the arms name the DESTINATION vocabulary.

    discovered_facilities calls them provider/power_mw. Without the
    renaming subquery the CASE references s.operator and s.capacity_mw
    against a table that has neither, and every sync run raises
    UndefinedColumn into a bare except.
    """
    sql = _built(CRAWLER, "_CAP_SYNC_ALL_SQL",
                 {"_cap_sync_sql": _load_cap_sync_sql()})
    derived = sql[sql.index(" FROM (SELECT "):]
    for col in ("operator", "capacity_mw", "status"):
        assert f"s.{col}" in sql, f"the arms stopped reading {col}"
        assert (f" AS {col}," in derived or f" AS {col} " in derived), (
            f"the derived table no longer exposes {col} — the arms would "
            f"reference s.{col} against a source that has no such column, "
            "and psycopg2 would raise UndefinedColumn on every sync")
    assert "df.provider AS operator" in derived, (
        "the provider→operator rename is gone")
    assert "df.power_mw AS capacity_mw" in derived, (
        "the power_mw→capacity_mw rename is gone")


def test_crawler_variants_differ_only_by_the_window():
    load = {"_cap_sync_sql": _load_cap_sync_sql()}
    all_sql = _built(CRAWLER, "_CAP_SYNC_ALL_SQL", load)
    recent = _built(CRAWLER, "_CAP_SYNC_RECENT_SQL", dict(
        load, _CAP_SYNC_WINDOW_7D=_built(CRAWLER, "_CAP_SYNC_WINDOW_7D")))
    window = _built(CRAWLER, "_CAP_SYNC_WINDOW_7D")
    assert recent != all_sql, "both sync variants built the same statement"
    assert recent.replace(window, "") == all_sql, (
        "the 7-day variant now differs from the full sweep by more than its "
        "window — they were unified precisely so classification cannot land "
        "in one and not the other")
    assert "7 days" in window, f"the 7-day window changed: {window!r}"


def test_both_crawler_call_sites_execute_the_built_sql():
    tree = _tree(CRAWLER)
    for fn_name, const in (("_run_market_refresh", "_CAP_SYNC_ALL_SQL"),
                           ("_run_facility_discovery",
                            "_CAP_SYNC_RECENT_SQL")):
        names = _executed_names(_func(tree, fn_name))
        assert const in names, (
            f"{fn_name} no longer executes {const} — either it went back to "
            f"an inline unclassified INSERT or the constant is dead: {names}")


def test_brain_writer_derives_its_insert_from_the_arms():
    tree = _tree(BRAIN)
    assert ("util.capacity_pipeline", "cp_classify_arms") in _imports(tree), (
        "autonomous_brain no longer imports cp_classify_arms")
    # The arms are bound to a name first so the SQL can stay one quote-free
    # f-string; the derivation therefore lives on that binding.
    assert _calls(_assign(tree, "_CAP_ARMS_S", BRAIN).value,
                  "cp_classify_arms"), (
        "_CAP_ARMS_S stopped deriving from cp_classify_arms() — the brain "
        "writer is a private copy of the taxonomy again")
    node = _assign(tree, "_BRAIN_CAP_INSERT_SQL", BRAIN)
    names = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
    assert "_CAP_ARMS_S" in names, (
        "_BRAIN_CAP_INSERT_SQL no longer interpolates _CAP_ARMS_S, so its "
        "CASE is not the canonical taxonomy")


def test_brain_insert_classifies_and_placeholders_match_the_call_site():
    from util.capacity_pipeline import cp_classify_arms
    sql = _built(BRAIN, "_BRAIN_CAP_INSERT_SQL",
                 {"_CAP_ARMS_S": cp_classify_arms("s")})
    assert sql.startswith("INSERT INTO capacity_pipeline"), sql[:60]
    assert "data_flag)" in sql.split(" SELECT ")[0], (
        "data_flag left the brain writer's INSERT column list")
    assert "CASE " + cp_classify_arms("s") + " ELSE NULL END" in sql, (
        "the brain writer's CASE is not the canonical arms")
    _no_restatement(sql, "_BRAIN_CAP_INSERT_SQL")
    assert sql.rstrip().endswith("ON CONFLICT DO NOTHING"), (
        "the brain writer lost its ON CONFLICT guard")

    # The parameters are named by a one-row subquery; a bare %s there has no
    # inferable type, so every cast must survive.
    for cast in ("%s::text AS operator", "%s::real AS capacity_mw",
                 "%s::text AS status"):
        assert cast in sql, (
            f"the brain writer dropped a required cast ({cast!r}) — Postgres "
            'answers "could not determine data type of parameter"')

    n_ph = sql.count("%s")
    assert sql.count("%") == n_ph, (
        "the brain writer's SQL carries a % that is not a placeholder; "
        "psycopg2 substitutes into this statement and would error every run")
    call = [n for n in ast.walk(_func(_tree(BRAIN), "extract_capacity_from_news"))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "execute" and n.args
            and isinstance(n.args[0], ast.Name)
            and n.args[0].id == "_BRAIN_CAP_INSERT_SQL"]
    assert len(call) == 1, (
        "extract_capacity_from_news does not execute _BRAIN_CAP_INSERT_SQL "
        "exactly once — the classified statement is dead or duplicated")
    passed = call[0].args[1]
    assert isinstance(passed, ast.Tuple) and len(passed.elts) == n_ph, (
        f"the writer binds {len(getattr(passed, 'elts', []))} parameters to "
        f"{n_ph} placeholders — psycopg2 raises, and the bare except in this "
        "function turns that into a silently skipped insert")


def _load_cap_sync_sql():
    """The real _cap_sync_sql, compiled alone (no crawler_scheduler import).

    Its source half lives in _cap_sync_source, so both are compiled into one
    namespace; that split is what keeps the destination shell quote-free for
    the idempotency rule (see test_writers_keep_on_conflict_visible_to_lint).
    """
    tree = _tree(CRAWLER)
    ns = {"cp_classify_arms": cp_classify_arms_ref(),
          "_CAP_SYNC_COLS": _built(CRAWLER, "_CAP_SYNC_COLS")}
    for name in ("_cap_sync_source", "_cap_sync_sql"):
        fn = _func(tree, name)
        exec(compile(ast.Module(body=[fn], type_ignores=[]), CRAWLER, "exec"),
             ns)
    return ns["_cap_sync_sql"]


def test_writers_keep_on_conflict_visible_to_the_idempotency_lint():
    """Both statements must LOOK idempotent to regression_lint, not just be it.

    scripts/regression_lint.py scans forward from `INSERT INTO <table>` with
    `[^;"']*` — it stops dead at the first quote character. A SQL literal or
    a plain concatenation seam between the INSERT and its ON CONFLICT hides
    the clause, and `--mode delta` then BLOCKS the PR on a writer that is
    already idempotent. That is why the crawler's source scan is split into
    _cap_sync_source and the brain's arms are bound to _CAP_ARMS_S first:
    both keep the destination shell quote-free. Re-inlining either reads as
    a harmless tidy-up and breaks the gate, so pin it with the real regex.
    """
    import re
    pattern = re.compile(r"INSERT\s+INTO\s+(\w+)[^;\"']*", re.I)
    checked = 0
    for path in (CRAWLER, BRAIN):
        with open(path, encoding="utf-8") as f:
            src = f.read()
        hits = [m for m in pattern.finditer(src)
                if m.group(1).lower() == "capacity_pipeline"]
        assert hits, (
            f"{os.path.basename(path)} no longer spells INSERT INTO "
            "capacity_pipeline where the linter can see it — if the writer "
            "moved, move this guard; if the table name was hidden behind a "
            "variable, put it back (that suppresses a real safety rule)")
        for m in hits:
            checked += 1
            assert "ON CONFLICT" in m.group(0).upper(), (
                f"{os.path.basename(path)}: regression_lint's scan from "
                "`INSERT INTO capacity_pipeline` ends before ON CONFLICT — a "
                "quote character now sits between them, so `--mode delta` "
                "will report insert-no-on-conflict and block the PR. Keep "
                f"the statement's shell quote-free. Scanned: {m.group(0)[:120]!r}")
    assert checked >= 2, f"expected both writers, scanned {checked}"


# ── 5 · coverage is self-policing ────────────────────────────────────


#: Writers that INSERT INTO capacity_pipeline and are NOT classified,
#: because they cannot run at all. Each entry is the measurement that showed
#: it dead — re-measure before deleting an entry, and classify the writer
#: (do not extend this map) if it is ever repaired.
#: Empty since #2802 (2026-08-17) deleted both recorded writers outright —
#: global_intelligence_agent.track_capacity_pipeline (SELECT died on SQLite
#: `datetime()`) and pipeline_drafts_api.approve_draft (INSERT named six
#: columns the live table does not have). Their entries were left behind by
#: that PR, which turned test_dead_writer_records_still_point_at_real_writers
#: red on main; the guard was right and the registry was stale.
#:
#: Keep the mechanism. A writer that provably cannot run belongs here with the
#: measurement that proved it, not silently unclassified.
_DEAD_WRITERS = {}

_SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__",
              "tests", "site-packages", "build", "dist"}


def _writer_files():
    hits = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            try:
                with open(path, encoding="utf-8") as f:
                    src = f.read()
            except (OSError, UnicodeDecodeError):
                continue
            if "INSERT INTO capacity_pipeline" in src:
                hits.append((os.path.relpath(path, ROOT), src))
    return hits


def test_every_capacity_pipeline_writer_classifies_or_is_recorded_dead():
    hits = _writer_files()
    # Floor was 4 until #2802 deleted two writers outright; 3 survive
    # (extractor_cron, crawler_scheduler, autonomous_brain). The number is a
    # vacuity tripwire, not a census — an empty scan passes everything below.
    assert len(hits) >= 3, (
        "the writer scan found almost nothing — it stopped matching (an "
        f"empty scan passes every assertion below): {[h[0] for h in hits]}")
    unclassified = []
    for rel, src in hits:
        if "cp_classify_arms" in src:
            continue
        if os.path.basename(rel) in _DEAD_WRITERS:
            continue
        unclassified.append(rel)
    assert not unclassified, (
        "these modules INSERT INTO capacity_pipeline without classifying the "
        f"row: {unclassified}. A writer that does not stamp puts its rows "
        "into the served SUM (COALESCE(data_flag,'')='') until a human next "
        "runs repair_capacity_pipeline_quarantine.py --apply — the drift "
        "this whole guard set exists to stop. Either derive a data_flag from "
        "util.capacity_pipeline.cp_classify_arms, or, if the writer cannot "
        "run, add it to _DEAD_WRITERS with the measurement that proved it.")


def test_dead_writer_records_still_point_at_real_writers():
    """A stale exemption is worse than none — it hides a live writer."""
    present = {os.path.basename(rel) for rel, _ in _writer_files()}
    stale = [name for name in _DEAD_WRITERS if name not in present]
    assert not stale, (
        f"_DEAD_WRITERS exempts {stale}, which no longer INSERT INTO "
        "capacity_pipeline. Drop the entry — left in place it would silently "
        "exempt a future writer that reuses the filename.")
