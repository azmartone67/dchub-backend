"""r-mps-archive-drift (2026-08-08) — the archive must be able to hold the row
it is archiving, and must never delete a row it did not copy.

THE BUG: dchub_self_heal.py::fix_collapse_history archived with

    INSERT INTO market_power_scores_history SELECT * FROM market_power_scores

`SELECT *` is not a column list. It is a positional bet that the two tables
keep the same arity forever, and the archive was created by an earlier
`LIKE market_power_scores INCLUDING ALL` that froze it at the 26 columns the
live table had that day. Measured on the live database 2026-08-08:

    market_power_scores            30 columns
    market_power_scores_history    26 columns
    live-only  data_basis_json (jsonb), iso_type, signal_tier, method_version

so every run since died at parse time. Not caught, not surfaced: the
`except Exception` returns (False, str(e)) and the caller writes it to
self_heal_events, which counted

    collapse_history  True  25,651   2026-05-11 10:29 .. 2026-06-02 08:34
    collapse_history  False 10,098   2026-06-02 08:38 .. 2026-08-08 15:36

— a four-minute changeover on 2026-06-02 when data_basis_json landed, and 67
days of identical red after it, still failing while this was written.

NOTHING WAS LOST, verified three ways before the fix was written:
  * psycopg2 raises at the INSERT, so the DELETE under it never ran; and the
    archive, both DELETEs and the counts share one transaction, so a row
    cannot be deleted unless its copy landed.
  * UNIQUE(market_slug) (twice over: _slug_key and _slug_unique) has held the
    live table at one row per slug — 330 rows, 330 distinct slugs — so both
    predicates match zero rows regardless.
  * `EXPLAIN` of the shipped statement inside a READ ONLY transaction returns
    the same "INSERT has more expressions than target columns" recorded in
    self_heal_events, i.e. it fails in analysis, before any executor runs.

WHY A HARDCODED COLUMN LIST WOULD BE WORSE: it fails silently. The next
ADD COLUMN on the live table simply stops being archived, no error anywhere,
and the archive quietly becomes a partial copy — the same class as the six
hand-copied INSERT lists in util/dcpi_score_row.py, only without the loud
crash that made this one findable. So the list is derived from the catalog and
the archive widens itself to match.

WHAT IS PINNED HERE:
  1. NO `SELECT *` in the archive path, ever again.
  2. The INSERT's column list and the SELECT's are byte-identical — the
     positional-drift class, killed by construction rather than counted.
  3. A column added to the live table is REPAIRED (ALTER on the archive) and
     then ARCHIVED — divergence between the two tables' column sets resolves
     instead of crashing or silently dropping.
  4. `id` is never copied: both tables draw it from the same sequence.
  5. Every DELETE in fix_collapse_history is preceded by an archive of the
     SAME predicate, read out of the AST. This is the data-loss guard, and it
     is the one assertion here that would have failed on the shipped code.
"""

import ast
import os

import pytest

import dchub_self_heal as sh


# The two tables as measured on the live database 2026-08-08, attnum order.
LIVE_COLUMNS = [
    ("id", "integer"), ("market_slug", "text"), ("market_name", "text"),
    ("state", "text"), ("iso", "text"), ("latitude", "double precision"),
    ("longitude", "double precision"), ("constraint_score", "real"),
    ("excess_power_score", "real"), ("time_to_power_months", "real"),
    ("queue_capacity_mw", "real"), ("queue_wait_months", "real"),
    ("reserve_margin_pct", "real"), ("gen_additions_12mo_mw", "real"),
    ("curtailment_pct", "real"), ("stranded_capacity_mw", "real"),
    ("emergency_count_30d", "integer"), ("top_risks_json", "text"),
    ("top_opportunities_json", "text"), ("verdict", "text"),
    ("tier_required", "text"), ("computed_at", "timestamp with time zone"),
    ("trend_30d", "real"), ("published", "boolean"), ("quality_score", "real"),
    ("avg_kwh_cents", "real"),
    # The four that broke it, in the order they were added.
    ("data_basis_json", "jsonb"), ("iso_type", "text"),
    ("signal_tier", "text"), ("method_version", "text"),
]

# The archive as it stands in production: the first 26, frozen.
HISTORY_COLUMNS = LIVE_COLUMNS[:26]

MISSING_FROM_HISTORY = ["data_basis_json", "iso_type", "signal_tier",
                        "method_version"]


def _plan():
    return sh.archive_reconcile_plan(LIVE_COLUMNS, HISTORY_COLUMNS)


# ---------------------------------------------------------------------------
# 1. the divergence the live database actually has
# ---------------------------------------------------------------------------

def test_reconcile_names_exactly_the_four_columns_history_is_missing():
    adds, _shared = _plan()
    assert [name for name, _ in adds] == MISSING_FROM_HISTORY


def test_reconcile_carries_the_live_type_not_a_guess():
    """data_basis_json is jsonb. An archive that widened it to text would
    accept the row and quietly change what a reader gets back."""
    adds, _ = _plan()
    assert dict(adds)["data_basis_json"] == "jsonb"


def test_add_column_sql_is_additive_and_idempotent():
    adds, _ = _plan()
    stmts = sh.archive_add_column_sql(adds)
    assert len(stmts) == len(MISSING_FROM_HISTORY)
    for stmt in stmts:
        assert stmt.startswith("ALTER TABLE market_power_scores_history ")
        assert "ADD COLUMN IF NOT EXISTS" in stmt
        # Never anything that could rewrite or lose a stored value.
        for verb in ("DROP", "ALTER COLUMN", "SET NOT NULL", "USING"):
            assert verb not in stmt


def test_identical_schemas_need_no_repair():
    adds, shared = sh.archive_reconcile_plan(LIVE_COLUMNS, LIVE_COLUMNS)
    assert adds == []
    assert len(shared) == len(LIVE_COLUMNS) - 1   # id dropped


def test_history_only_columns_are_left_alone():
    """A column the archive has and live does not is not named by the INSERT,
    so it keeps its default instead of failing the statement."""
    extra = HISTORY_COLUMNS + [("archived_at", "timestamp with time zone")]
    adds, shared = sh.archive_reconcile_plan(LIVE_COLUMNS, extra)
    assert "archived_at" not in shared
    assert "archived_at" not in [n for n, _ in adds]


# ---------------------------------------------------------------------------
# 2. the statement itself
# ---------------------------------------------------------------------------

def _column_lists(sql):
    """(insert_list, select_list) as written in the generated statement."""
    insert_part = sql.split("INSERT INTO market_power_scores_history (", 1)[1]
    insert_cols = insert_part.split(")", 1)[0]
    select_cols = sql.split("SELECT ", 1)[1].split(" FROM ", 1)[0]
    return insert_cols.strip(), select_cols.strip()


def test_archive_never_uses_select_star():
    """The defect, named. `SELECT *` in this statement is what shipped, what
    broke on 2026-06-02, and what 10,098 self_heal_events rows recorded."""
    _adds, shared = _plan()
    for predicate in (sh.ARCHIVE_PREDICATE_STALE, sh.ARCHIVE_PREDICATE_TIE):
        sql = sh.archive_sql(shared, predicate)
        assert "SELECT *" not in sql
        assert "SELECT  *" not in sql


def test_insert_and_select_name_the_same_columns_in_the_same_order():
    _adds, shared = _plan()
    insert_cols, select_cols = _column_lists(
        sh.archive_sql(shared, sh.ARCHIVE_PREDICATE_STALE))
    assert insert_cols == select_cols
    assert insert_cols.count(",") == len(shared) - 1


def test_archive_covers_every_live_column_except_id():
    """The whole point of the repair: after the ALTERs, nothing on the live
    row is dropped on the way into the archive."""
    _adds, shared = _plan()
    live_names = [n for n, _ in LIVE_COLUMNS]
    assert set(shared) == set(live_names) - {"id"}
    # Ordering follows the live table, so a reviewer can diff the two.
    assert shared == [n for n in live_names if n != "id"]


def test_id_is_never_copied_into_the_archive():
    """Both tables default id from market_power_scores_id_seq, and their id
    ranges already interleave (history 1..1438, live starts at 1346), so a
    copied id is a PK violation waiting for one collision."""
    _adds, shared = _plan()
    assert "id" not in shared
    sql = sh.archive_sql(shared, sh.ARCHIVE_PREDICATE_STALE)
    insert_cols, _ = _column_lists(sql)
    assert '"id"' not in insert_cols


def test_archive_and_delete_are_built_from_the_same_predicate():
    _adds, shared = _plan()
    for predicate in (sh.ARCHIVE_PREDICATE_STALE, sh.ARCHIVE_PREDICATE_TIE):
        assert predicate in sh.archive_sql(shared, predicate)
        assert predicate in sh.archive_delete_sql(predicate)


def test_tie_predicate_breaks_on_id_not_ctid():
    """ctid moves when a row is updated, so the archiving SELECT and the
    DELETE could resolve to different sets; the primary key cannot."""
    assert "ctid" not in sh.ARCHIVE_PREDICATE_TIE
    assert "b.id > m.id" in sh.ARCHIVE_PREDICATE_TIE


def test_empty_column_list_refuses_rather_than_emitting_bare_insert():
    with pytest.raises(ValueError):
        sh.archive_sql([], sh.ARCHIVE_PREDICATE_STALE)


# ---------------------------------------------------------------------------
# 3. the next column, which is the one this is really for
# ---------------------------------------------------------------------------

def test_a_new_live_column_is_repaired_and_archived_not_dropped():
    """The forward-looking half. Add a column to the live table only — the
    exact shape of the 2026-06-02 break — and the plan must both widen the
    archive AND name the column in the INSERT. A hardcoded list passes the
    first half of this and fails the second, silently."""
    future = LIVE_COLUMNS + [("carbon_intensity_g", "real")]
    adds, shared = sh.archive_reconcile_plan(future, HISTORY_COLUMNS)
    assert ("carbon_intensity_g", "real") in adds
    assert "carbon_intensity_g" in shared
    sql = sh.archive_sql(shared, sh.ARCHIVE_PREDICATE_STALE)
    assert sql.count('"carbon_intensity_g"') == 2       # INSERT and SELECT


def test_column_sets_that_diverge_do_not_produce_a_mismatched_statement():
    """Whatever the two tables look like, the generated INSERT and SELECT
    agree in arity — the failure mode that took the archive down cannot be
    reached from this builder."""
    cases = [
        (LIVE_COLUMNS, HISTORY_COLUMNS),                     # today
        (LIVE_COLUMNS, LIVE_COLUMNS),                        # repaired
        (LIVE_COLUMNS, [("id", "integer")]),                 # near-empty archive
        (LIVE_COLUMNS[:3], LIVE_COLUMNS),                    # archive is wider
    ]
    for live, hist in cases:
        _adds, shared = sh.archive_reconcile_plan(live, hist)
        insert_cols, select_cols = _column_lists(
            sh.archive_sql(shared, sh.ARCHIVE_PREDICATE_STALE))
        assert insert_cols == select_cols


# ---------------------------------------------------------------------------
# 4. identifiers and types reach DDL escaped, or not at all
# ---------------------------------------------------------------------------

def test_identifiers_are_quoted_and_embedded_quotes_doubled():
    assert sh._quote_ident("verdict") == '"verdict"'
    assert sh._quote_ident('we"ird') == '"we""ird"'


def test_a_type_that_is_not_a_type_is_refused_not_spliced():
    with pytest.raises(ValueError):
        sh.archive_add_column_sql([("x", "text; DROP TABLE market_power_scores")])
    with pytest.raises(ValueError):
        sh.archive_add_column_sql([("x", "")])


def test_real_postgres_types_are_accepted():
    for typ in ("text", "jsonb", "real", "boolean", "double precision",
                "numeric(10,2)", "character varying(255)",
                "timestamp with time zone", "text[]"):
        assert sh.archive_add_column_sql([("c", typ)])


# ---------------------------------------------------------------------------
# 5. the data-loss guard, read out of the shipped function
# ---------------------------------------------------------------------------

def _collapse_fn_ast():
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "dchub_self_heal.py")
    with open(src, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "fix_collapse_history":
            return node
    raise AssertionError("fix_collapse_history not found in dchub_self_heal.py")


def _predicate_name(call_node, index):
    """Name of the predicate constant passed to an archive/delete builder."""
    arg = call_node.args[index]
    return arg.id if isinstance(arg, ast.Name) else "<not-a-constant>"


def test_every_delete_is_preceded_by_an_archive_of_the_same_predicate():
    """The assertion that fails on the code this replaces.

    Walked in source order: each archive_delete_sql(P) must have an
    archive_sql(..., P) before it. A future edit that reorders them, drops the
    archive, or points the DELETE at a different predicate deletes rows with
    no copy — which is the only way this function can lose data.
    """
    fn = _collapse_fn_ast()
    archived, deletes = set(), []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id == "archive_sql":
            archived.add((node.lineno, _predicate_name(node, 1)))
        elif node.func.id == "archive_delete_sql":
            deletes.append((node.lineno, _predicate_name(node, 0)))

    assert deletes, "fix_collapse_history deletes nothing - guard is vacuous"
    assert archived, "fix_collapse_history archives nothing"

    for line, predicate in deletes:
        assert any(a_pred == predicate and a_line < line
                   for a_line, a_pred in archived), (
            "DELETE on %s at line %d has no archive of the same predicate "
            "before it - rows would be deleted without a copy" % (predicate, line))


def test_collapse_runs_no_raw_delete_on_the_live_table():
    """Belt to the guard above's braces: the predicate check only sees the
    builders, so a hand-written DELETE string would slip past it."""
    fn = _collapse_fn_ast()
    for node in ast.walk(fn):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            # Both sides upper-cased. Folding only the haystack is how the
            # first draft of this assertion passed against a mutation that
            # put the raw DELETE straight back in.
            assert "DELETE FROM MARKET_POWER_SCORES" not in node.value.upper(), (
                "raw DELETE literal in fix_collapse_history at line %d - "
                "route it through archive_delete_sql so the archive guard "
                "can see it" % node.lineno)


def test_archive_and_delete_rowcounts_are_reconciled_before_commit():
    """The runtime half: matching predicates are necessary, agreeing rowcounts
    are the proof. Without the raise, a desync commits the DELETE anyway."""
    fn = _collapse_fn_ast()
    raises = [n for n in ast.walk(fn) if isinstance(n, ast.Raise)]
    assert raises, ("fix_collapse_history never raises - an archive/delete "
                    "rowcount mismatch would commit silently")


def test_on_conflict_do_nothing_is_paired_with_the_rowcount_check():
    """The interlock, pinned so it cannot be half-removed.

    ON CONFLICT DO NOTHING on an archive is a data-loss clause on its own: a
    row that conflicts is silently not copied, and the DELETE removes the
    original regardless. It is only defensible next to a rowcount comparison
    that turns the skip into a rollback. Whoever deletes the check must also
    delete the clause, and this fails either way round.
    """
    _adds, shared = _plan()
    sql = sh.archive_sql(shared, sh.ARCHIVE_PREDICATE_STALE)
    if "ON CONFLICT" not in sql.upper():
        return                      # no clause, nothing to interlock
    fn = _collapse_fn_ast()
    reconciles = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Compare)
        and any(isinstance(op, ast.NotEq) for op in n.ops)
        and "archived" in ast.dump(n) and "deleted" in ast.dump(n)
    ]
    assert reconciles, (
        "archive_sql emits ON CONFLICT DO NOTHING but fix_collapse_history no "
        "longer compares the archived rowcount against the deleted one - a "
        "conflicting row would be dropped and its original deleted anyway")
