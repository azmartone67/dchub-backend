"""2026-08-08 — /api/v1/fiber/summary published six confident zeros.

Measured live on a KEYLESS endpoint (allow-listed in free_tier_gate.py:187):

    fiber_routes: 0             while /api/v1/stats said 55,079 and the server
                               card, llms.txt and the MCP handshake all
                               advertise 55,064
    legacy_metro_dark_fiber: 0 while /api/v1/stats said 59, from a table of
                               the same name
    legacy_fiber_routes: 0
    major_hubs: 0
    subsea_planned: 0
    last_sync: null

Two causes, and the second is the one that generalises:

1. WRONG TABLE — `fiber_routes` was counted from `fiber_route_geometry`, not
   the `fiber_routes` table that /api/v1/stats reads.

2. `except Exception: stats[...] = 0` on every count. A missing table, a
   permission error and a genuinely empty table produced identical output. A
   zero is a MEASUREMENT; absence must be null.

These tests read the shipped source with `ast`/regex rather than importing the
app, per the house rule that tests never import main.py.
"""
import ast
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "fiber_integration.py"


@pytest.fixture(scope="module")
def src():
    return SRC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def summary_fn(src):
    """Just the body of fiber_intelligence_summary."""
    start = src.index("def fiber_intelligence_summary")
    nxt = src.find("\n    @app.route", start)
    end = nxt if nxt != -1 else src.find("\n    logger.info", start)
    assert end > start, "could not bound fiber_intelligence_summary"
    return src[start:end]


def test_fiber_routes_counts_the_canonical_table(summary_fn):
    """It must count the SAME table /api/v1/stats counts, or the two surfaces
    publish different numbers under one name."""
    assert "FROM fiber_route_geometry" not in summary_fn, (
        "fiber_route_geometry is the wrong table — it published 0 against an "
        "advertised 55,064")
    assert re.search(r"SELECT COUNT\(\*\) FROM fiber_routes\b", summary_fn), (
        "fiber_routes must be counted from the fiber_routes table, matching "
        "main.py's total_fiber_routes")


def test_no_count_falls_back_to_a_fabricated_zero(summary_fn):
    """The whole bug class: on failure, produce null — never 0.

    ★The first version of this test only matched ASSIGNMENTS (`... = 0`) inside
    an except block. Mutation-testing caught that it sailed straight past
    `return 0, None` in the helper — which is the MOST likely regression shape,
    since that is where the failure value is now produced. Cover both forms.
    """
    offenders = []
    for m in re.finditer(r"except[^\n]*:\s*\n(?:[ \t]+[^\n]*\n){0,6}", summary_fn):
        block = m.group(0)
        if re.search(r"=\s*0\s*(?:#.*)?$", block, re.M):
            offenders.append("assigns 0: " + block.strip()[:110])
        if re.search(r"\breturn\s+0\b", block):
            offenders.append("returns 0: " + block.strip()[:110])
    assert not offenders, (
        "an except handler produces 0 — absence must be null, never a measured "
        "zero:\n  " + "\n  ".join(offenders))


def test_failed_reads_are_null_and_declared(summary_fn):
    """A null must be accompanied by a reason, so a reader can tell 'we could
    not read this' from 'we measured zero'."""
    assert "unavailable" in summary_fn, (
        "the response must declare which fields could not be read")
    assert re.search(r"stats\[key\]\s*=\s*value", summary_fn), (
        "counts must be assigned from the helper's return value, which is None "
        "on failure")
    assert "basis" in summary_fn, (
        "each count must name the table it came from")


def test_rollback_on_failure_so_one_bad_read_cannot_cascade(summary_fn):
    """In PostgreSQL a failed statement aborts the transaction and every later
    statement on that connection fails too — which turns ONE missing table into
    a page of zeros. That is the most likely mechanism behind six of them."""
    assert "rollback()" in summary_fn, (
        "a failed count must roll back, or it poisons every subsequent read")


def test_last_sync_is_not_a_phantom_key(summary_fn):
    """`stats.get('last_sync')` read a key nothing in the function ever set, so
    it was unconditionally null while implying a sync clock existed."""
    # Match the CODE usage (dict-value position), not any mention: the comment
    # documenting this fix necessarily quotes the old expression, and a guard
    # that scans for the bare string flags the explanation of its own fix.
    assert not re.search(r"'last_sync'\s*:\s*stats\.get\(", summary_fn), (
        "last_sync was read from a key never assigned — say so explicitly "
        "instead of implying a tracked sync time")
    assert "last_sync_note" in summary_fn, (
        "if last_sync is null, the response must say why")


# ═══════════════════════════════════════════════════════════════════════
# 2026-08-08, SECOND PASS — A SUCCESSFUL READ THAT IS STILL NOT A
# MEASUREMENT
#
# The fix above separates "the read failed" (null) from "we counted" (a
# number). It cannot see the third case, which is what `major_hubs: 0` and
# `subsea_planned: 0` were:
#
#     SELECT COUNT(*) FROM subsea_landing_points WHERE is_major_hub = TRUE
#
# No exception, no null, a correct 0 — over a column that is FALSE on all
# 1,927 rows and TRUE on none. Verified live 2026-08-08: ?major_only=true
# returns 0 rows, and for subsea_cables BOTH ?planned=true and
# ?planned=false return 0, which only happens when every value is NULL.
#
# ★ THE HARD PART IS NOT SUPPRESSING THE ZERO. It is suppressing ONLY the
# unmeasured ones. A column that genuinely varies and yields 0 has been
# measured, and nulling that would trade one lie for another — so the
# genuine-zero case is tested first and hardest below.
# ═══════════════════════════════════════════════════════════════════════

class _FakeCursor:
    """Minimal psycopg2-cursor stand-in for the population probe.

    `util.db_honesty` is a leaf module (psycopg2 is imported lazily inside
    open_conn), so the real predicate can be executed here rather than
    grepped for — a regex can only prove the call site exists, not that it
    decides correctly.
    """

    def __init__(self, row=None, raises=None):
        self._row, self._raises, self.executed = row, raises, []
        self.connection = self
        self.rolled_back = False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if self._raises:
            raise self._raises

    def fetchall(self):
        return [self._row]

    def rollback(self):
        self.rolled_back = True


@pytest.fixture(scope="module")
def honesty():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import util.db_honesty as mod
    return mod


def test_a_genuine_zero_is_still_published(honesty):
    """★ THE ANTI-OVERREACH TEST — it must fail if the guard gets greedy.

    Some rows TRUE, some FALSE, and the filtered count is 0: we looked and
    found none. That is a real finding and MUST survive as 0. A guard that
    nulls this has replaced a false zero with a false gap.
    """
    cur = _FakeCursor(row=(1000, 1000, 2))       # rows, non_null, distinct
    ok, reason = honesty.zero_is_measured(cur, "some_table", "some_flag")
    assert ok is True, (
        "a column that VARIES yields a measured zero — suppressing it "
        "invents a coverage gap that does not exist")
    assert reason is None


def test_all_null_column_is_never_a_measured_zero(honesty):
    """subsea_cables.is_planned — NULL on all 699 rows."""
    cur = _FakeCursor(row=(699, 0, 0))
    ok, reason = honesty.zero_is_measured(cur, "subsea_cables", "is_planned")
    assert ok is False, (
        "is_planned is unset on every row; 0 counts rows nothing could match")
    assert "699" in reason and "never populated" in reason, (
        "the reason must quote what was observed so a reader can check it, "
        f"got: {reason!r}")


def test_constant_column_is_never_a_measured_zero(honesty):
    """subsea_landing_points.is_major_hub — FALSE on all 1,927 rows, TRUE on
    none. Non-null, so a COUNT()-based check alone would call it populated."""
    cur = _FakeCursor(row=(1927, 1927, 1))
    ok, reason = honesty.zero_is_measured(
        cur, "subsea_landing_points", "is_major_hub")
    assert ok is False, (
        "a flag with one distinct value and no matches carries no signal")
    assert "1,927" in reason, f"reason must quote the row count, got {reason!r}"


def test_empty_string_column_counts_as_unpopulated(honesty):
    """landing_points.country is '' on every row, not NULL. Plain COUNT()
    treats '' as present, so the text probe must strip it — otherwise a
    country breakdown gets built on an empty column."""
    cur = _FakeCursor(row=(1927, 0, 0))
    verdict, _ = honesty.column_population(
        cur, "subsea_landing_points", "country", "text")
    assert verdict == honesty.NEVER_SET
    sql = cur.executed[0][0]
    assert "NULLIF" in sql and "BTRIM" in sql, (
        "the text probe must discount empty/whitespace strings, or '' reads "
        f"as populated data; got SQL: {sql}")


def test_empty_table_keeps_its_honest_zero(honesty):
    """No rows at all — 0 is a true statement about an empty table."""
    cur = _FakeCursor(row=(0, 0, 0))
    ok, _ = honesty.zero_is_measured(cur, "t", "c")
    assert ok is True, "an empty table honestly contains zero matching rows"


def test_a_failed_probe_does_not_invent_a_gap(honesty):
    """If the probe itself cannot run we have no evidence the column is dead.
    Demoting a count to null on no evidence is its own fabrication."""
    cur = _FakeCursor(raises=RuntimeError("boom"))
    ok, reason = honesty.zero_is_measured(cur, "t", "c")
    assert ok is True and reason is None
    assert cur.rolled_back, (
        "a failed probe must roll back or it poisons every later read on the "
        "same connection — the cascade this endpoint already suffered once")


def test_probe_identifiers_must_be_literals(honesty):
    """Table/column are interpolated into SQL, so they must never be able to
    come from a request. Validated rather than escaped."""
    with pytest.raises(ValueError):
        honesty.column_population(_FakeCursor(row=(1, 1, 1)),
                                  "t; DROP TABLE x", "c")
    with pytest.raises(ValueError):
        honesty.column_population(_FakeCursor(row=(1, 1, 1)),
                                  "t", "c = TRUE OR 1=1")


# ── the endpoint must actually USE the predicate ──────────────────────

@pytest.fixture(scope="module")
def counts_table(src):
    """The COUNTS tuple, evaluated from source (no import of the app)."""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "COUNTS" for t in node.targets):
            return ast.literal_eval(node.value)
    pytest.fail("COUNTS table not found in fiber_integration.py")


def test_every_filtered_count_carries_a_population_probe(counts_table):
    """★ THE STRUCTURAL GUARD. Any count with a WHERE clause can return a 0
    that means 'this column was never populated'. Adding such a count without
    a probe is exactly how major_hubs and subsea_planned shipped, so a new one
    must not be able to arrive unguarded.

    An unfiltered COUNT(*) needs no probe — its 0 means the table is empty,
    which is a true statement.
    """
    missing = []
    for entry in counts_table:
        key, sql, _table, probe = entry
        if "WHERE" in sql.upper() and not probe:
            missing.append(key)
    assert not missing, (
        "these counts filter on a column but cannot tell a measured zero from "
        "a never-populated one: " + ", ".join(missing))


def test_each_probe_names_a_column_its_query_actually_filters_on(counts_table):
    """A probe pointed at the wrong column certifies nothing — it would clear
    a zero by measuring a different column that happens to be populated."""
    wrong = []
    for key, sql, _table, probe in counts_table:
        if not probe:
            continue
        table, column, _kind = probe
        if table not in sql or column not in sql:
            wrong.append(f"{key}: probes {table}.{column}, absent from its SQL")
    assert not wrong, "\n  ".join(wrong)


def test_probe_runs_only_when_the_count_is_zero(summary_fn):
    """A non-zero count is self-evidently backed by a populated column, so the
    extra round trip is waste. Also documents the intended trigger."""
    assert re.search(r"value\s*==\s*0", summary_fn), (
        "the population probe must be gated on a zero count")
    assert "zero_is_measured" in summary_fn, (
        "the endpoint must consult the shared predicate, not hand-roll one")


def test_an_unpopulated_column_becomes_null_and_is_declared(summary_fn):
    """The whole point: demote to null AND say why, in the same
    `unavailable[]` list a failed read uses. A null with no reason is only
    marginally better than a zero."""
    assert re.search(r"value,\s*err\s*=\s*None,\s*why", summary_fn), (
        "when the probe rejects a zero, the value must become None and carry "
        "the reason into unavailable[]")


# ── end-to-end: the real handler, against a stub DB ───────────────────
#
# Everything above tests the predicate and the source structure. Neither
# proves the LOOP wires them together — that a rejected zero actually reaches
# the response as null. So pull the shipped function out with `ast` and run
# it (house rule: tests never import main.py).

def _load_summary_handler(src, db):
    """exec the real fiber_intelligence_summary against stubs."""
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef)
               and n.name == "fiber_intelligence_summary"), None)
    assert fn is not None, "handler not found"
    fn.decorator_list = []                      # drop @app.route
    module = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(module)

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    ns = {"get_db": lambda: db, "jsonify": lambda payload: payload}
    exec(compile(module, "<fiber_integration>", "exec"), ns)
    return ns["fiber_intelligence_summary"]


class _StubDB:
    """Answers exact SQL strings; anything unlisted raises UndefinedTable."""

    def __init__(self, answers):
        self.answers, self.seen = answers, []

    # connection API
    def cursor(self):
        return self

    def rollback(self):
        pass

    def close(self):
        pass

    @property
    def connection(self):
        return self

    # cursor API
    def execute(self, sql, params=None):
        norm = " ".join(sql.split())
        self.seen.append(norm)
        if norm not in self.answers:
            raise RuntimeError(f"UndefinedTable: {norm}")
        self._row = self.answers[norm]

    def fetchall(self):
        return [self._row]

    def fetchone(self):
        return self._row


@pytest.fixture(scope="module")
def live_shaped_response(src):
    """The response for a DB shaped like production on 2026-08-08."""
    answers = {
        # counts that really are measured
        "SELECT COUNT(*) FROM subsea_cables": (699,),
        "SELECT COUNT(*) FROM subsea_landing_points": (1927,),
        "SELECT COUNT(*) FROM carrier_profiles": (286,),
        "SELECT COUNT(*) FROM carrier_facility_presence": (609022,),
        "SELECT COUNT(DISTINCT dchub_facility_id) FROM "
        "carrier_facility_presence WHERE dchub_facility_id IS NOT NULL":
            (15660,),
        "SELECT COUNT(*) FROM fiber_routes": (55079,),
        "SELECT COUNT(*) FROM fiber_coverage_zones": (2831,),
        "SELECT COUNT(*) FROM fiber_coverage_zones WHERE dark_fiber_available "
        "= TRUE": (622,),
        "SELECT COUNT(*) FROM metro_dark_fiber": (59,),
        # the two unmeasured zeros
        "SELECT COUNT(*) FROM subsea_cables WHERE is_planned = TRUE": (0,),
        "SELECT COUNT(*), COUNT(is_planned), COUNT(DISTINCT is_planned) FROM "
        "subsea_cables": (699, 0, 0),          # all NULL
        "SELECT COUNT(*) FROM subsea_landing_points WHERE is_major_hub = TRUE":
            (0,),
        "SELECT COUNT(*), COUNT(is_major_hub), COUNT(DISTINCT is_major_hub) "
        "FROM subsea_landing_points": (1927, 1927, 1),   # all FALSE
        # long_haul_fiber_routes deliberately absent -> genuine read failure
    }
    db = _StubDB(answers)
    return _load_summary_handler(src, db)(), db


def test_endtoend_unpopulated_columns_publish_null_not_zero(
        live_shaped_response):
    body, _ = live_shaped_response
    stats = body["stats"]
    assert stats["major_hubs"] is None, (
        "is_major_hub is FALSE on all 1,927 rows and TRUE on none — that is a "
        f"coverage gap, not a count; got {stats['major_hubs']!r}")
    assert stats["subsea_planned"] is None, (
        "is_planned is NULL on all 699 rows; got "
        f"{stats['subsea_planned']!r}")


def test_endtoend_every_null_is_declared_with_a_reason(live_shaped_response):
    body, _ = live_shaped_response
    declared = {u["field"]: u["reason"] for u in body["unavailable"]}
    for field in ("major_hubs", "subsea_planned"):
        assert field in declared, f"{field} is null but not in unavailable[]"
        assert "measured zero" in declared[field], (
            f"{field}'s reason must say what the null means, got "
            f"{declared[field]!r}")
    # and the ordinary read failure still behaves as before
    assert body["stats"]["legacy_fiber_routes"] is None
    assert "legacy_fiber_routes" in declared

    for key, value in body["stats"].items():
        if value is None:
            assert key in declared, f"{key} is null with no declared reason"


def test_endtoend_measured_values_are_untouched(live_shaped_response):
    """★ The guard must not disturb anything it was not aimed at."""
    body, _ = live_shaped_response
    assert body["stats"]["fiber_routes"] == 55079, (
        "the cross-check against /api/v1/stats.total_fiber_routes must hold")
    assert body["stats"]["dark_fiber_zones"] == 622, (
        "a filtered count over a POPULATED column keeps its value")
    assert body["stats"]["subsea_cables"] == 699
    assert body["stats"]["landing_points"] == 1927


def test_endtoend_nulled_fields_are_dropped_from_basis(live_shaped_response):
    """`basis` names where a MEASUREMENT came from. A field we could not
    measure must not claim a source table."""
    body, _ = live_shaped_response
    for field in ("major_hubs", "subsea_planned", "legacy_fiber_routes"):
        assert field not in body["basis"], (
            f"{field} is null but still advertises a basis table")


def test_endtoend_probe_is_skipped_for_nonzero_counts(live_shaped_response):
    """dark_fiber_zones is 622, so its column is obviously populated — the
    extra round trip must not be issued."""
    _, db = live_shaped_response
    probes = [s for s in db.seen if "COUNT(DISTINCT dark_fiber_available)" in s]
    assert not probes, f"probed a non-zero count: {probes}"


def test_basis_note_no_longer_claims_null_means_only_read_failure(summary_fn):
    """The response asserted 'a null value means the read failed' while
    publishing two zeros that were not measurements. Whatever the note says
    must stay true of what the code does."""
    note = summary_fn[summary_fn.index("'basis_note'"):]
    note = note[:note.index("'data_sources'")] if "'data_sources'" in note \
        else note
    assert "never populated" in note, (
        "the basis note must describe the second reason a field is null, or "
        "it contradicts the response it annotates")
