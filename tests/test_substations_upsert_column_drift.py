"""Guard: the substations upsert must name columns the live table actually has.

WHAT THIS PINS
──────────────
`_upsert_substations` in land_power_crawler.py named SIXTEEN columns, THREE of
which do not exist on the live `substations` table. Verified against
information_schema on 2026-07-30 — the house rule is that the live table is the
truth and the repo DDL is not:

    named in the INSERT   live column        status
    hifld_id              hifld_objectid     ABSENT — renamed
    lon                   lng                ABSENT — renamed
    last_updated          updated_at         ABSENT — renamed

`ON CONFLICT (hifld_id)` named the same absent column. So every row of every
batch would raise UndefinedColumn.

★ THE SMOKING GUN IS STILL IN THE SCHEMA. The unique index is NAMED
  `substations_hifld_id_uniq` but is defined ON `hifld_objectid`:
      CREATE UNIQUE INDEX substations_hifld_id_uniq
        ON public.substations USING btree (hifld_objectid)
  The column was renamed; the index name and this crawler were both left behind.
  (Live carries two more unique indexes on the same column. The DDL in this repo
  is a 18-column subset of a 36-column live table — it is not the schema of
  record, which is exactly how this survived.)

★★ THIS IS THE SECOND FAULT, NOT THE ONE THAT FIRES — and that distinction is
   the reusable lesson, learned the hard way in #1933 where a client-side
   psycopg2 binding error masked an UndefinedColumn I had already published as
   the cause. Measured here BEFORE writing the fix:

     HIFLD_SUBSTATIONS_URL -> HTTP 500
     {"errors":{"message":"Item does not exist or is inaccessible."}}

   The upstream ArcGIS dataset is gone, so `_fetch_geojson_stream` raises and the
   INSERT is never reached. land_power_sync_log agrees — every `hifld-substations`
   row records fetched=0, upserted=0, errors=1, "Fatal: 500 Server Error", most
   recently 2026-03-30. This statement has therefore NEVER executed against the
   live table and the drift has never once fired.

   So this repair is LANDMINE REMOVAL, not a restoration. It is worth doing
   because whoever revives the feed must not also have to rediscover three
   renamed columns — but it does NOT make the crawler work, and nothing here
   claims it does.

★ THE TABLE IS NOT STALE. `substations` holds 126,842 rows (79,686 with
  source='HIFLD'), maintained by hifld_substation_loader.py — which writes `lng`
  correctly — with updated_at as recent as 2026-07-30. This crawler is
  SUPERSEDED, not load-bearing. `full_refresh` is accepted and never used, so
  there is no DELETE and no wipe risk either way.

THE CONTRACT
────────────
  S1. Every column the INSERT names exists on the live substations table.
  S2. ON CONFLICT targets a column that carries a UNIQUE index.
  S3. A failing upsert reports WHAT failed, not just how many times. The old
      `except Exception as e: errors += 1` bound `e` and never read it, so a
      100%-failing batch was indistinguishable from a 100%-succeeding one in
      everything except a count nobody surfaced.
  S4. Fetched-rows-but-wrote-zero is logged at ERROR, never as a quiet success.
      (Its sibling eia-860-plants reported errors=0 while dropping 54,934 of
      55,000 records — that is how a 66-row table got published for months.)
  S5. The repo DDL in this same module declares the names the INSERT uses. They
      drifted apart precisely because CREATE TABLE IF NOT EXISTS is a no-op
      against an existing table, so nothing ever forced them to agree.

EXPECTED PASS/FAIL — MEASURED, not predicted.
─────────────────────────────────────────────
Measured by extracting origin/main @ 06bda3fc with `git archive`, dropping this
file into that tree, and running it there.

UNPATCHED (origin/main @ 06bda3fc):   6 failed, 2 passed, 1 xfailed
    The 2 that pass unpatched pass in BOTH states and exist to prove the harness
    works rather than to pin the patch:
        test_extraction_parsed_and_free_vars_resolve
        test_harness_rejects_the_original_column_names   (positive control)
PATCHED (this branch):                0 failed, 8 passed, 1 xfailed

`1 xfailed` on BOTH runs. A conftest-level abort or a collection error exits 0
with 0 tests and renders as an ordinary red job (it shipped twice on 2026-07-28
and left the backend with no test gate for hours) — the control's presence in
the summary is the only proof this file ran.

Run:  python3 -m pytest tests/test_substations_upsert_column_drift.py -v

═══════════════════════════════════════════════════════════════════════════════
2026-08-02 — S6/S7/S8: THE FEED CAME BACK AND THE LANDMINE FIRED
═══════════════════════════════════════════════════════════════════════════════
The header above says this statement "has NEVER executed against the live table".
That expired when #1996 revived the fetch. It has now executed — 75,328 times in
one run — and /api/land-power/status reported:

    fetched=75,328   upserted=2,000   errors=73,328   duration_s=4,740

★ ALL THREE COUNTS ARE WRONG, AND NOT IN THE DIRECTION ANYONE WOULD GUESS.
  Verified against the live table the same day: 0 rows created, 0 rows updated,
  count unmoved at 126,846. The run wrote NOTHING.
  · upserted=2,000 counted cur.execute() calls that returned. The caller holds
    ONE transaction for all 76 batches; the first UniqueViolation aborted it and
    `conn.commit()` on an aborted transaction is a ROLLBACK that does not raise.
  · errors=73,328 is ONE error plus 73,327 InFailedSqlTransaction. A per-row
    try/except around statements that share a transaction is isolation theatre.
  · duration=4,740s is 73,327 round-trips that could not succeed, 15x the
    dispatcher's 300s budget — and the request keeps running server-side long
    after curl gives up, which is why hifld-transmission stopped logging.

★ WHY EXACTLY 2,000 — the number is the tell, not a coincidence. The 2026-07-31
  canary in routes/substation_ingest.py ran with cap=2000 and, for the 1,330 of
  those rows that matched on (name, lat, lng), CORRECTED the held row's
  hifld_objectid to the real upstream ID. Its 670 inserts were reverted; those
  1,330 corrections were not. Live today: exactly 1,330 rows carry
  hifld_objectid in 107,655..110,133, all stamped 2026-07-31 03:04:47. So
  upstream rows 1..2,000 all "succeeded" and row 2,001 — one past the canary's
  reach — collided. The success count was the width of a two-day-old canary.

★ THE ARBITER WAS NEVER THE BUG, so do not "fix" it. ON CONFLICT
  (hifld_objectid) matches a real full index. The table carries EIGHT unique
  indexes and ON CONFLICT arbitrates ONE. And this is NOT the partial-index
  trap: substations_name_lat_lng_uniq is NOT partial (two siblings are —
  ix_substations_name_lat_lng and idx_substations_hifld_oid — so that trap is
  live in this table, just not what fires here).

  S6. One bad row does not poison the batch — a real SAVEPOINT, not try/except.
  S7. The count reported equals the count that survives COMMIT.
  S8. crawl_substations refuses the write while identity is unresolved, the
      same refusal routes/substation_ingest.py already returns as a 409.

MEASURED against origin/main @ 1d850102 (git archive + this file dropped in):
UNPATCHED:  3 failed, 8 passed, 1 xfailed   (S6, S7, S8 — and only those)
PATCHED:    0 failed, 11 passed, 1 xfailed
Unpatched S7 fails with "reported upserted=2 but only 0 row(s) survive COMMIT",
which is the production defect at scale 5.
"""
import ast
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAND = os.path.join(ROOT, "land_power_crawler.py")

FN = "_upsert_substations"

# The LIVE substations column set, read from information_schema 2026-07-30.
# 36 columns; the repo DDL declares a subset. Reproduced verbatim so the stub
# rejects exactly what Postgres rejects.
LIVE_COLUMNS = {
    "id", "name", "operator", "substation_type", "voltage_kv", "capacity_mva",
    "lat", "lng", "city", "state", "country", "connected_transmission",
    "status", "source", "source_id", "created_at", "updated_at",
    "hifld_objectid", "zip", "county", "naics_code", "naics_desc",
    "source_date", "val_method", "val_date", "type", "lines", "min_volt",
    "max_volt", "owner", "county_fips", "max_voltage_kv", "min_voltage_kv",
    "sub_type", "lines_count", "available_mva",
}

# Columns carrying a UNIQUE index live (ON CONFLICT needs one of these).
LIVE_UNIQUE_COLUMNS = {"id", "hifld_objectid", "source_id"}

# The three renames, kept as data so a failure message can name them.
RENAMES = {"hifld_id": "hifld_objectid", "lon": "lng",
           "last_updated": "updated_at"}


# ── extraction ────────────────────────────────────────────────────────────────
def _extract(name=FN):
    """AST-extract a top-level function, decorators stripped.

    Assert the parse produced a Module with a non-empty body AND that the
    function body is non-empty — an empty parse satisfies every downstream
    assertion silently, which is the failure mode an isinstance filter hides.
    """
    tree = ast.parse(open(LAND).read())
    assert isinstance(tree, ast.Module), "parse did not produce a Module"
    assert tree.body, "parsed module body is EMPTY — extraction read nothing"
    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == name), None)
    assert fn is not None, f"{name} not found in {LAND}"
    assert fn.body, f"{name} parsed with an EMPTY body"
    fn.decorator_list = []
    return fn, tree


def _arg_names(a):
    names = [x.arg for x in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)]
    for v in (a.vararg, a.kwarg):
        if v:
            names.append(v.arg)
    return names


def _free_vars(fn):
    assigned, loaded = set(), set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Name):
            (assigned if isinstance(n.ctx, ast.Store) else loaded).add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                assigned.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.FunctionDef):
            if n is not fn:
                assigned.add(n.name)
            assigned.update(_arg_names(n.args))
        elif isinstance(n, ast.Lambda):
            assigned.update(_arg_names(n.args))
        elif isinstance(n, ast.ExceptHandler) and n.name:
            assigned.add(n.name)
    import builtins
    return sorted(loaded - assigned - set(dir(builtins)))


def _insert_sql(fn):
    for n in ast.walk(fn):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            if re.search(r"(?i)INSERT\s+INTO\s+substations", n.value):
                return " ".join(n.value.split())
    return None


def _named_columns(sql):
    """The column list between `INSERT INTO substations (` and `)`."""
    m = re.search(r"(?i)INSERT INTO substations\s*\((.*?)\)\s*VALUES", sql)
    assert m, f"could not parse the column list out of: {sql[:120]}"
    out = []
    for tok in m.group(1).split(","):
        t = tok.strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", t):
            out.append(t)
    return out


ABORTED = ("current transaction is aborted, commands ignored until end of "
           "transaction block")


# ── stub cursor — rejects exactly what Postgres rejects ───────────────────────
class _Cur:
    """Models the column rules AND the transaction state machine.

    ★ 2026-08-02: this stub used to model statements in isolation, so it could
    not see the defect that actually cost production a run — a failed statement
    POISONS THE TRANSACTION. Every later statement raises InFailedSqlTransaction
    until something rolls back, and a commit() on an aborted transaction throws
    away the rows that did succeed. A stub with no transaction state certifies a
    per-row try/except as "isolated" when Postgres gives it no isolation at all;
    that is exactly how `upserted=2,000` was reported for a run that wrote zero.
    """

    def __init__(self, log, fail_rows=()):
        self.log = log
        self.aborted = False        # transaction is in the ERROR state
        self.savepoints = []
        self.inserts = 0            # INSERT attempts seen
        self.committed = 0          # INSERTs still live (survive a commit)
        self.fail_rows = set(fail_rows)   # 0-based INSERT indices to fail

    def _guard(self):
        if self.aborted:
            raise RuntimeError(ABORTED)

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.log.append(s)
        up = s.upper()

        # ── transaction control ──────────────────────────────────────────────
        if up.startswith("ROLLBACK TO"):
            name = s.split()[-1]
            if name not in self.savepoints:
                raise RuntimeError(f'no such savepoint: "{name}"')
            # THE POINT OF THE WHOLE MECHANISM: this — and only this — clears
            # the error state and lets the loop keep going.
            self.aborted = False
            return
        if up.startswith("SAVEPOINT"):
            self._guard()
            self.savepoints.append(s.split()[-1])
            return
        if up.startswith("RELEASE"):
            self._guard()
            name = s.split()[-1]
            if name not in self.savepoints:
                raise RuntimeError(f'no such savepoint: "{name}"')
            return
        if up.startswith(("COMMIT", "BEGIN", "ROLLBACK")):
            self._guard()
            return

        self._guard()

        # psycopg2 runs Python %-formatting CLIENT-SIDE when params are passed,
        # so an un-escaped literal % raises before the SQL is ever sent. Model
        # the BINDING before the column logic — a stub more forgiving than the
        # driver certifies code the driver rejects (learned in #1933).
        if params is not None:
            try:
                sql % tuple(params)
            except (IndexError, TypeError, ValueError) as exc:
                raise IndexError(f"{exc} — literal % must be escaped as %%") from exc

        def _fail(msg):
            # Every statement error aborts the enclosing transaction. Set the
            # flag BEFORE raising, exactly as the server does.
            self.aborted = True
            raise RuntimeError(msg)

        for col in _named_columns(s):
            if col not in LIVE_COLUMNS:
                _fail(f'column "{col}" of relation "substations" does not exist')
        m = re.search(r"(?i)ON CONFLICT\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)", s)
        if m and m.group(1) not in LIVE_UNIQUE_COLUMNS:
            _fail(f'there is no unique or exclusion constraint matching the '
                  f'ON CONFLICT specification ("{m.group(1)}")')
        for col in re.findall(r"(?i)SET\s+([a-z_]+)\s*=|,\s*([a-z_]+)\s*=\s*EXCLUDED", s):
            for c2 in col:
                if c2 and c2 not in LIVE_COLUMNS:
                    _fail(f'column "{c2}" does not exist')

        if up.startswith("INSERT"):
            idx = self.inserts
            self.inserts += 1
            if idx in self.fail_rows:
                # The live failure, verbatim: a unique index this statement's
                # ON CONFLICT does not arbitrate.
                _fail('duplicate key value violates unique constraint '
                      '"substations_name_lat_lng_uniq"')
            self.committed += 1

    def surviving(self):
        """Rows still present after the caller's conn.commit().

        ★ COMMIT ON AN ABORTED TRANSACTION IS A ROLLBACK. Postgres discards
        everything and psycopg2 does not raise, which is why a run could report
        2,000 upserts, exit cleanly, and leave the table untouched. Any count
        the crawler publishes must agree with THIS, not with how many
        cur.execute() calls happened to return.
        """
        return 0 if self.aborted else self.committed


def _run(nrows=3, fail_rows=()):
    """Execute the extracted upsert against the stub. Returns its return value."""
    fn, _ = _extract()
    log = []
    ns = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), LAND, "exec"), ns)
    row = ("123", "SUB A", "OP", "TX", "Harris", "Houston",
           29.7, -95.4, 345.0, 345.0, 138.0, "TAP", "operational", 3)
    cur = _Cur(log, fail_rows=fail_rows)
    return ns[FN](cur, [row] * nrows), log, cur


# ── sanity + positive control (pass in both states) ───────────────────────────
def test_extraction_parsed_and_free_vars_resolve():
    fn, tree = _extract()
    assert len(tree.body) > 1, "module parsed to a single node — not this file"
    unresolved = set(_free_vars(fn))
    assert not unresolved, (
        f"unresolved free vars in {FN}: {sorted(unresolved)} — this function is "
        f"meant to be self-contained apart from its two arguments")


def test_harness_rejects_the_original_column_names():
    """The stub must reject hifld_id/lon/last_updated, or S1 is vacuous."""
    bad = ("INSERT INTO substations (hifld_id, name, lat, lon, last_updated) "
           "VALUES (%s, %s, %s, %s, NOW())")
    with pytest.raises(RuntimeError, match="does not exist"):
        _Cur([]).execute(bad, ("1", "n", 1.0, 2.0))
    for old, new in RENAMES.items():
        assert old not in LIVE_COLUMNS, f"stub wrongly accepts {old}"
        assert new in LIVE_COLUMNS, f"stub wrongly rejects {new}"
    # and the ON CONFLICT target check has teeth
    with pytest.raises(RuntimeError, match="ON CONFLICT"):
        _Cur([]).execute("INSERT INTO substations (name) VALUES (%s) ON CONFLICT DO NOTHING "
                         "ON CONFLICT (hifld_id) DO NOTHING", ("n",))


# ── S1 ────────────────────────────────────────────────────────────────────────
def test_every_named_column_exists_on_the_live_table():
    fn, _ = _extract()
    sql = _insert_sql(fn)
    assert sql, "no INSERT INTO substations found"
    named = _named_columns(sql)
    assert named, "parsed an empty column list"
    missing = [c for c in named if c not in LIVE_COLUMNS]
    assert not missing, (
        f"INSERT names {len(missing)} column(s) absent from the live table: "
        f"{missing}. Renames: {RENAMES}")


# ── S2 ────────────────────────────────────────────────────────────────────────
def test_on_conflict_targets_a_column_with_a_unique_index():
    fn, _ = _extract()
    sql = _insert_sql(fn)
    m = re.search(r"(?i)ON CONFLICT\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)", sql)
    assert m, f"no ON CONFLICT target in: {sql[:120]}"
    target = m.group(1)
    assert target in LIVE_COLUMNS, f"ON CONFLICT ({target}) — column absent"
    assert target in LIVE_UNIQUE_COLUMNS, (
        f"ON CONFLICT ({target}) — that column carries no unique index live, so "
        f"Postgres raises 'no unique or exclusion constraint matching'")


# ── S1+S2 executed ────────────────────────────────────────────────────────────
def test_the_upsert_actually_runs_against_a_live_shaped_cursor():
    result, log, cur = _run(nrows=3)
    assert log, "the statement never reached the cursor"
    assert isinstance(result, tuple) and len(result) >= 2, \
        f"{FN} returned {result!r}"
    upserted, errors = result[0], result[1]
    assert errors == 0, f"{errors} row(s) still failed against the live schema"
    assert upserted == 3, f"expected 3 upserts, got {upserted}"


# ── S3 ────────────────────────────────────────────────────────────────────────
def test_a_failing_row_reports_what_failed_not_just_a_count():
    fn, _ = _extract()
    src = ast.unparse(fn)
    # the old form bound `e` and never read it
    assert not re.search(r"except Exception as (\w+):\s*\n\s*errors \+= 1\s*\n\s*(return|$)",
                         src), "the error message is still discarded"
    result, _, _cur = _run(nrows=2)
    assert len(result) >= 3, (
        f"{FN} returns {len(result)} values — a caller cannot learn WHY rows "
        f"failed, only how many did. Return the first error message too.")

    class _Boom:
        def execute(self, sql, params=None):
            raise RuntimeError("column \"nope\" does not exist")
    ns = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), LAND, "exec"), ns)
    up, err, msg = ns[FN](_Boom(), [("x",) * 14] * 2)[:3]
    assert up == 0 and err == 2
    assert msg and "nope" in msg, (
        f"a 100%-failing batch surfaced {msg!r} — indistinguishable from a "
        f"100%-succeeding one except by a count nobody publishes")


# ── S4 ────────────────────────────────────────────────────────────────────────
def test_fetched_but_wrote_zero_is_logged_as_an_error():
    fn, _ = _extract("crawl_substations")
    # ast.unparse normalises `not upserted` to `(not upserted)`, so match the
    # unparsed form rather than the source text.
    src = ast.unparse(fn)
    m = re.search(r"if fetched and \(?not upserted\)?", src)
    assert m, ("no fetched-but-wrote-zero branch — a run that pulled rows and "
               "wrote none still reports as a success line")
    assert "logger.error" in src[m.start():m.start() + 400], \
        "the zero-write branch does not log at ERROR"


# ── S5 ────────────────────────────────────────────────────────────────────────
def test_repo_ddl_declares_the_same_names_the_insert_uses():
    """CREATE TABLE IF NOT EXISTS is a no-op against an existing table, so
    nothing ever forced these two to agree. That is why they drifted."""
    fn, _ = _extract("init_land_power_tables")
    ddl = None
    for n in ast.walk(fn):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            if re.search(r"(?i)CREATE TABLE IF NOT EXISTS substations", n.value):
                ddl = n.value
    assert ddl, "no substations CREATE TABLE found"
    body = ddl[ddl.index("("):]
    for old, new in RENAMES.items():
        assert not re.search(rf"(?m)^\s*{old}\s+\w", body), (
            f"repo DDL still declares `{old}`; live (and the INSERT) use "
            f"`{new}`. A fresh deploy would create a table the INSERT cannot "
            f"write to.")
        assert re.search(rf"(?m)^\s*{new}\s+\w", body), \
            f"repo DDL does not declare `{new}`"


# ── S6 ────────────────────────────────────────────────────────────────────────
def test_one_bad_row_does_not_poison_the_rest_of_the_batch():
    """The 2026-08-02 production failure, in miniature.

    Row 0 hits a unique index ON CONFLICT does not arbitrate. Without a
    savepoint the transaction is aborted and rows 1..N each raise
    InFailedSqlTransaction — which is how ONE error was published as 73,328.
    """
    result, log, cur = _run(nrows=5, fail_rows={0})
    upserted, errors = result[0], result[1]
    assert errors == 1, (
        f"{errors} errors from ONE bad row — the other {errors - 1} are "
        f"InFailedSqlTransaction cascade, not independent failures. "
        f"A cascade reported as a population is what made 4,740s of "
        f"guaranteed-to-fail round-trips look like 73,328 real problems.")
    assert upserted == 4, f"expected the 4 good rows to land, got {upserted}"
    assert any(s.upper().startswith("ROLLBACK TO") for s in log), \
        "no ROLLBACK TO SAVEPOINT — the failed row was never undone"


# ── S7 ────────────────────────────────────────────────────────────────────────
def test_the_reported_count_equals_what_survives_commit():
    """★ THE HEADLINE DEFECT. `upserted=2,000` was published for a run that
    wrote nothing: the counter tracked cur.execute() calls that returned, and
    the aborted transaction's commit() silently threw them all away."""
    for fail_rows in ({}, {0}, {2}, {0, 3}):
        result, _log, cur = _run(nrows=5, fail_rows=set(fail_rows))
        assert result[0] == cur.surviving(), (
            f"fail_rows={fail_rows or 'none'}: reported upserted={result[0]} "
            f"but only {cur.surviving()} row(s) survive COMMIT. A count that "
            f"can outrun the commit is how this feed reported four months of "
            f"upserts into a table that never changed.")


# ── S8 ────────────────────────────────────────────────────────────────────────
def test_crawl_substations_cannot_reach_the_write_while_identity_is_unresolved():
    """The admin route refuses this write with a 409; the crawler is the second
    door to the same table and was left open. Both must refuse together."""
    src = open(LAND).read()
    assert re.search(r"^SUBSTATION_WRITES_BLOCKED\s*=\s*True", src, re.M), (
        "SUBSTATION_WRITES_BLOCKED is not set — crawl_substations will write "
        "~25,000 duplicate substations under UNKNOWN<id> names. Resolve "
        "identity first (see routes/substation_ingest.py).")
    fn, _ = _extract("crawl_substations")
    body = ast.unparse(fn)
    guard = body.find("SUBSTATION_WRITES_BLOCKED")
    write = body.find("_upsert_substations")
    assert guard != -1, "crawl_substations does not consult the guard"
    assert write != -1, "the write machinery was deleted rather than gated"
    assert guard < write, \
        "the guard is checked AFTER the write — it gates nothing"
    assert "_ingest_conn" in body and guard < body.find("_ingest_conn"), (
        "the guard fires after the ingest connection is opened — a blocked run "
        "should not take a connection it will not use")


# ── must-fail control — never delete ─────────────────────────────────────────
@pytest.mark.xfail(strict=True, reason="must-fail control: proves this file runs")
def test_zzz_must_fail_control():
    assert False, "control"
