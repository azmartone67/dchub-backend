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


# ---------------------------------------------------------------------------
# 4. what the fixer REPORTS — r-archive-noop-honesty (2026-08-08)
# ---------------------------------------------------------------------------
#
# fix_collapse_history returned ok=True with `before=330 archived=0 deleted=0
# after=330` every ~6 h, which renders as FIXED. It had never archived a row
# and could not: both predicates need two rows to exist for one slug, and
# market_power_scores carries TWO UNIQUE (market_slug) indexes, so every
# writer is UPDATE-in-place. Measured live 2026-08-08 — 330 rows, 0 slugs with
# more than one row, archive frozen at 1,346 rows since 2026-05-11 (#2437).
#
# The fixer stays armed (the constraint it depends on is maintained by a
# sibling fixer, so the no-op is not permanent by construction), but the
# report now distinguishes "nothing to do BECAUSE nothing can be" from
# "nothing to do THIS TIME, and the guard rail is gone".
#
# These run the SHIPPED function against a stub connection. An assertion about
# its AST would pass on a body that still returned the bare counts.

class _FakeCursor:
    def __init__(self, before, after, archived, deleted, uniques):
        self._before, self._after = before, after
        self._archived, self._deleted = list(archived), list(deleted)
        self._uniques = list(uniques)
        self._counts_served = 0
        self._last = ""
        self.rowcount = 0
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append(sql)
        self._last = sql
        up = sql.upper()
        if up.lstrip().startswith("INSERT INTO MARKET_POWER_SCORES_HISTORY"):
            self.rowcount = self._archived.pop(0)
        elif up.lstrip().startswith("DELETE FROM MARKET_POWER_SCORES"):
            self.rowcount = self._deleted.pop(0)
        else:
            self.rowcount = 0

    def fetchone(self):
        self._counts_served += 1
        return (self._before if self._counts_served == 1 else self._after,)

    def fetchall(self):
        if "indisunique" in self._last:
            # Full (name, nkeyatts, is_partial, first_col) rows, so the
            # shipped rule runs here rather than being bypassed by a stub
            # that hands back only the names it already approved of.
            return [r if isinstance(r, tuple) else (r, 1, False, "market_slug")
                    for r in self._uniques]
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _collapse(monkeypatch, *, archived=(0, 0), deleted=(0, 0),
              before=330, after=330, uniques=()):
    cur = _FakeCursor(before, after, archived, deleted, uniques)
    monkeypatch.setattr(sh, "DATABASE_URL", "postgresql://stub", raising=False)
    monkeypatch.setattr(sh, "_conn", lambda: _FakeConn(cur))
    cols = {"market_power_scores": LIVE_COLUMNS,
            "market_power_scores_history": HISTORY_COLUMNS}
    monkeypatch.setattr(sh, "_table_columns", lambda _cur, t: cols[t])
    ok, details = sh.fix_collapse_history()
    return ok, details, cur


def test_harness_reports_real_work_in_the_original_shape(monkeypatch):
    """Anti-vacuous: when the fixer DOES archive, nothing about the report
    changes. Without this, a body that returned a fixed string would satisfy
    both branch tests below."""
    ok, details, _ = _collapse(monkeypatch, archived=(2, 1), deleted=(2, 1),
                               before=333, after=330,
                               uniques=["market_power_scores_slug_key"])
    assert ok is True
    assert details.startswith("before=333 archived=3 deleted=3 after=330")
    assert "noop" not in details and "armed" not in details


def test_a_structural_noop_does_not_report_as_work_done(monkeypatch):
    """The defect: `before=330 archived=0 deleted=0 after=330` rendered as
    FIXED for a job that cannot act."""
    ok, details, _ = _collapse(
        monkeypatch, uniques=["market_power_scores_slug_key",
                              "market_power_scores_slug_unique"])
    assert ok is True, "a no-op is not a failure — ok=False would land a "\
                       "working fixer in fixes_fail and success=false"
    assert details.startswith("noop:"), details
    assert not details.startswith("before="), (
        "a no-op still leads with a bare count line, which is what read as "
        "work done"
    )


def test_the_noop_names_why_it_is_a_noop(monkeypatch):
    """A bare 'noop' is only marginally better than a bare zero — the report
    has to carry the constraint that makes it one, because that constraint
    disappearing is the event that makes this fixer matter again."""
    ok, details, _ = _collapse(
        monkeypatch, uniques=["market_power_scores_slug_key",
                              "market_power_scores_slug_unique"])
    assert "UNIQUE(market_slug)" in details
    for name in ("market_power_scores_slug_key",
                 "market_power_scores_slug_unique"):
        assert name in details, f"{name} missing from: {details}"
    assert "before=330" in details and "after=330" in details, (
        "the counts must survive — they are still the audit trail"
    )


def test_a_missing_guard_rail_reports_armed_not_noop(monkeypatch):
    """★ The dangerous branch, and the one people forget to test.

    Zero archived with NO unique index is a completely different statement
    from zero archived with one: duplicates are possible, so this fixer is
    load-bearing and simply found nothing THIS cycle. Collapsing the two into
    one message is how a healer reports a healthy system while the invariant
    it depends on is gone.
    """
    ok, details, _ = _collapse(monkeypatch, uniques=[])
    assert ok is True
    assert details.startswith("armed:"), details
    assert "NO UNIQUE(market_slug)" in details
    assert "load-bearing" in details
    assert "noop" not in details, (
        "an unguarded table must not report as the steady state"
    )


def test_the_guard_rail_probe_asks_about_indexes_not_constraints(monkeypatch):
    """A bare CREATE UNIQUE INDEX enforces uniqueness just as hard as a
    constraint but has no pg_constraint row. Asking pg_constraint would report
    'no guard rail' while one is in force — a false alarm on the one line an
    operator is meant to trust."""
    _ok, _d, cur = _collapse(monkeypatch, uniques=["x"])
    probes = [s for s in cur.sql if "indisunique" in s]
    assert probes, "the fixer never asks whether uniqueness is enforced"
    probe = probes[0]
    assert "pg_index" in probe
    assert "pg_constraint" not in probe


# (name, nkeyatts, is_partial, first_col) — the shapes pg_index can return.
GUARD_RAIL = ("mps_slug_key", 1, False, "market_slug")
COMPOSITE = ("mps_slug_computed_key", 2, False, "market_slug")
PARTIAL = ("mps_slug_published_key", 1, True, "market_slug")
OTHER_COLUMN = ("mps_pkey", 1, False, "id")


def test_a_real_unique_index_on_the_slug_counts():
    """Anti-vacuous: a rule that counted nothing would pass every case below."""
    assert sh.slug_guard_rails([GUARD_RAIL]) == ["mps_slug_key"]


@pytest.mark.parametrize("row,why", [
    (COMPOSITE, "UNIQUE (market_slug, computed_at) permits many rows per slug "
                "— exactly the state this fixer exists to collapse"),
    (PARTIAL, "a partial unique index leaves duplicates legal outside its "
              "predicate"),
    (OTHER_COLUMN, "the primary key on id says nothing about market_slug"),
])
def test_shapes_that_are_unique_but_not_guard_rails_do_not_count(row, why):
    """★ Counting any of these silences the `armed` branch exactly when it is
    needed: the table would be reported as protected while duplicates are
    possible.

    Executed against the rule rather than matched against the SQL text — a
    WHERE clause can only be checked by substring, and `indpred IS NULL` ->
    `(indpred IS NULL OR true)` still contains the substring. That mutation
    survived the text version of this test.
    """
    assert sh.slug_guard_rails([row]) == [], why


def test_one_bad_shape_does_not_drag_a_good_one_out_with_it():
    assert sh.slug_guard_rails([COMPOSITE, GUARD_RAIL, PARTIAL, OTHER_COLUMN]) \
        == ["mps_slug_key"]


def test_the_probe_describes_every_unique_index_and_filters_none():
    """The SQL must NOT pre-filter, or `slug_guard_rails` never sees the rows
    it exists to reject and the rule becomes untested in production while
    still passing here."""
    probe = sh._UNIQUE_SLUG_SQL
    assert "indisunique" in probe
    assert "indnkeyatts" in probe and "indpred" in probe, (
        "the probe must SELECT the shape columns the rule decides on"
    )
    assert "indnkeyatts = 1" not in probe, (
        "filtering in SQL moves the rule back out of the tested function"
    )
