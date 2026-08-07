"""Guard: the ingestion-freshness board must convict a dead loader, must NEVER
convict a layer it merely failed to read, and must never report a truncate-and-
reload as growth.

FENCES routes/ingestion_freshness_master_shell.py. Every test drives the real
shipped function (_lane_for_layer, _population, _tick's verdict helper) against
a fake cursor. A comment or a renamed variable cannot satisfy them.

──────────────────────────────────────────────────────────────────────────
THE THREE PROPERTIES, each paired with a MUST-FAIL CONTROL so a vacuous test
cannot masquerade as a passing one:

1. A STALE LAYER CONVICTS. test_stale_layer_convicts drives a 132-day-old
   newest-write against a 120d cadence and asserts pass is False. Its control,
   test_fresh_layer_does_not_convict, drives the SAME harness with a fresh
   timestamp and asserts pass is True — so a test that convicted everything
   would fail the control.

2. UNREADABLE IS NOT STALE. Four separate read failures — column absent, column
   100% NULL, TEXT column that will not cast, database unreachable — must each
   render pass=None WITH a reason, never False. These are asserted as
   `is None`, not as falsy: `assert not passed` would pass on False and let the
   exact defect through.

3. ZERO IN 7d IS NOT A FAILURE. A layer on a quarterly cadence with 0 rows in
   7d and 0 in 30d, whose newest write is still inside its horizon, must PASS.
   This is the anti-cry-wolf property; a board that reds here gets deleted.

──────────────────────────────────────────────────────────────────────────
★ test_seed_load_is_not_a_rewrite FENCES A REAL BUG CAUGHT PRE-MERGE.
The first draft classified ingestion mode by "does the busiest single write-day
hold >=50% of rows". Run against live data that labelled `facilities` — the
healthiest loader on the platform, 1,366 genuinely new sites in 7d across 103
distinct write-days — as "TRUNCATES AND RELOADS", because its busiest day is an
old one-time SEED load holding 68% of rows. substations (63%) was mislabelled
the same way. A seed is indistinguishable from a reload by share-of-busiest-day.
The mode is now derived from the WINDOW DELTA's share of the table, which is the
sentence the mode exists to settle. This test drives the facilities shape and
asserts INCREMENTAL, so the share-of-busiest-day heuristic cannot come back.

Live figures, measured 2026-08-06 on the Neon read replica:
    facilities (DISTINCT canonical_slug) 16,742   newest 0.2d   +1,366 / 7d
    fiber_routes            55,064   newest 12.8d   +0 / 7d
    gas_pipelines           30,918   newest  3.8d   +30,000 / 7d  (REWRITE)
    power_plants            14,480   newest  7.0d   +14,414 / 7d  (REWRITE)
    transmission_lines      95,560   newest  2.9d   +95,553 / 7d  (REWRITE)
    substations            126,858   newest  0.2d   +16 / 7d
    interconnect_queue       5,483   newest  0.9d   +5,355 / 7d   (REWRITE)
    deals                    4,815   newest  0.0d   +90 / 7d
    subsea_cables              691   newest 132.9d  +0 / 7d       <- BEYOND
    subsea_landing_points    1,908   newest 132.9d  +0 / 7d       <- BEYOND

No DB and no network. Nothing runs at module scope.

Run locally:
    python3 -m pytest tests/test_ingestion_freshness_shell.py -v
"""
from __future__ import annotations

import datetime as _dt
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Live 2026-08-06 (Neon read replica).
LIVE_SUBSEA_CABLES = 691
LIVE_SUBSEA_AGE_DAYS = 132.9
LIVE_GAS_ROWS = 30918
LIVE_GAS_DELTA = 30000
LIVE_FACILITIES_DISTINCT = 16742
LIVE_FACILITIES_D7 = 1366
LIVE_FACILITIES_D30 = 2537
LIVE_FACILITIES_WRITE_DAYS = 103
LIVE_FACILITIES_BUSIEST = 16690  # ~68% of the 24,472 underlying rows: a SEED


def _mod():
    """Import the shell. An ImportError must NOT become a skip."""
    import routes.ingestion_freshness_master_shell as m
    return m


def _ago(days: float):
    return _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)


class _Cur:
    """Fake cursor dispatching on SQL shape. Mirrors the real query order:
    SET timeout -> count -> information_schema per candidate -> non-null probe
    -> freshness -> mode."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass

    def execute(self, sql):
        c = self.cfg
        if sql.startswith("SET "):
            self._row = None
            return
        if "information_schema" in sql:
            col = sql.split("column_name = '")[1].split("'")[0]
            dt = c["columns"].get(col)
            self._row = (dt,) if dt else None
            return
        if "COALESCE(MAX" in sql:                      # mode / write-day spread
            self._row = c.get("mode", (5, 10))
            return
        if "MAX(" in sql:                              # freshness
            if c.get("freshness_raises"):
                raise RuntimeError("DatatypeMismatch: invalid input syntax")
            self._row = c["freshness"]
            return
        # The non-null probe is the only statement selecting TWO counts, and it
        # also contains COUNT(*) — so it must be matched BEFORE the total.
        if "COUNT(*), COUNT(" in sql:                  # non-null probe
            if c.get("probe_raises"):
                raise RuntimeError("InvalidDatetimeFormat: bad text timestamp")
            self._row = (c.get("rows", c["total"]),
                         c.get("nonnull", c["total"]))
            return
        if "COUNT(*)" in sql or "COUNT(DISTINCT" in sql:   # total
            if c.get("count_raises"):
                raise RuntimeError("UndefinedColumn: boom")
            self._row = (c["total"],)
            return
        self._row = None

    def fetchone(self):
        return self._row


class _Conn:
    def __init__(self, cfg):
        self.cfg = cfg

    def cursor(self):
        return _Cur(self.cfg)

    def rollback(self):
        pass

    def close(self):
        pass


def _run(monkeypatch, spec, cfg):
    """Drive the REAL lane against a fake connection."""
    m = _mod()
    monkeypatch.setattr(m, "_conn", lambda: _Conn(cfg))
    return {c["id"].rsplit("_", 1)[-1]: c for c in m._lane_for_layer(spec)}


def _spec(**over):
    s = dict(key="x", label="test layer", table="t",
             ts_candidates=("created_at",), cadence_days=120,
             cadence="quarterly + 30d grace", source="test")
    s.update(over)
    return s


# ── 1. a stale layer convicts ───────────────────────────────────────────────
def test_stale_layer_convicts():
    """A loader silent past its own cadence is the failure this board exists
    for. Live shape: subsea_cables, 691 rows, newest write 132.9d ago."""
    import routes.ingestion_freshness_master_shell as m
    mp = pytest.MonkeyPatch()
    try:
        out = _run(mp, _spec(), {
            "total": LIVE_SUBSEA_CABLES,
            "columns": {"created_at": "timestamp with time zone"},
            "freshness": (_ago(LIVE_SUBSEA_AGE_DAYS), 0, 0),
            "mode": (1, LIVE_SUBSEA_CABLES)})
    finally:
        mp.undo()
    assert out["recency"]["pass"] is False, "a 132d-silent layer must convict"
    d = out["recency"]["detail"]
    assert "BEYOND CADENCE" in d
    # The row count must appear so the reader sees WHY count alone is useless.
    assert "691" in d
    assert m._lane_verdict(list(out.values())) == "FAIL"


def test_fresh_layer_does_not_convict():
    """MUST-FAIL CONTROL for the test above. Same harness, fresh timestamp.
    If the recency check convicted unconditionally, this fails."""
    mp = pytest.MonkeyPatch()
    try:
        out = _run(mp, _spec(), {
            "total": LIVE_SUBSEA_CABLES,
            "columns": {"created_at": "timestamp with time zone"},
            "freshness": (_ago(3.0), 5, 20),
            "mode": (9, 100)})
    finally:
        mp.undo()
    assert out["recency"]["pass"] is True
    assert "BEYOND CADENCE" not in out["recency"]["detail"]


def test_cadence_is_per_layer_not_global():
    """The SAME 40-day silence must pass a 120d quarterly layer and convict a
    14d continuous one. A single global threshold cannot do both — that is the
    over-broad scan this repo has already rejected twice."""
    mp = pytest.MonkeyPatch()
    cfg = {"total": 1000,
           "columns": {"created_at": "timestamp with time zone"},
           "freshness": (_ago(40.0), 0, 0), "mode": (3, 900)}
    try:
        quarterly = _run(mp, _spec(cadence_days=120), cfg)
        continuous = _run(mp, _spec(cadence_days=14), cfg)
    finally:
        mp.undo()
    assert quarterly["recency"]["pass"] is True
    assert continuous["recency"]["pass"] is False


# ── 2. unreadable is not stale (four failure modes) ─────────────────────────
def test_absent_column_is_unmeasurable_not_stale():
    mp = pytest.MonkeyPatch()
    try:
        out = _run(mp, _spec(ts_candidates=("created_at", "loaded_at")), {
            "total": 55064, "columns": {}, "freshness": (None, 0, 0)})
    finally:
        mp.undo()
    assert out["recency"]["pass"] is None, "absent column must be '?', not RED"
    d = out["recency"]["detail"]
    assert "UNMEASURABLE" in d
    assert "absent live" in d
    # rule 4: name the column that would be needed
    assert "created_at" in d and "loaded_at" in d


def test_all_null_column_is_unmeasurable_not_stale():
    mp = pytest.MonkeyPatch()
    try:
        out = _run(mp, _spec(), {
            "total": 500, "nonnull": 0,
            "columns": {"created_at": "timestamp with time zone"},
            "freshness": (None, 0, 0)})
    finally:
        mp.undo()
    assert out["recency"]["pass"] is None
    assert "100% NULL" in out["recency"]["detail"]


def test_uncastable_text_column_is_unmeasurable_not_stale():
    """deals.created_at and discovered_facilities.discovered_at are TEXT live.
    A TEXT column that will not cast must degrade, not convict and not 500."""
    mp = pytest.MonkeyPatch()
    try:
        out = _run(mp, _spec(), {
            "total": 4815, "probe_raises": True,
            "columns": {"created_at": "text"}, "freshness": (None, 0, 0)})
    finally:
        mp.undo()
    assert out["recency"]["pass"] is None
    assert "uncastable" in out["recency"]["detail"]


def test_empty_table_is_unmeasurable_and_says_so_distinctly():
    """An EMPTY table and an all-NULL column are different diagnoses — the
    first says the loader never wrote, the second says it wrote without
    stamping. Collapsing them sends the reader after the wrong loader.
    (cable_landing_points is live at 0 rows, hence the house rule.)"""
    mp = pytest.MonkeyPatch()
    try:
        out = _run(mp, _spec(), {
            "total": 0, "rows": 0, "nonnull": 0,
            "columns": {"created_at": "timestamp with time zone"},
            "freshness": (None, 0, 0)})
    finally:
        mp.undo()
    assert out["recency"]["pass"] is None
    d = out["recency"]["detail"]
    assert "EMPTY (0 rows)" in d
    assert "100% NULL" not in d


def test_db_unavailable_is_unmeasurable_not_stale():
    m = _mod()
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(m, "_conn", lambda: None)
        checks = m._lane_for_layer(_spec())
    finally:
        mp.undo()
    assert all(c["pass"] is None for c in checks)
    assert "not zero, not stale" in checks[0]["detail"]
    assert m._lane_verdict(checks) == "?"


def test_freshness_query_failure_is_unmeasurable_not_stale():
    mp = pytest.MonkeyPatch()
    try:
        out = _run(mp, _spec(), {
            "total": 100, "freshness_raises": True,
            "columns": {"created_at": "timestamp with time zone"}})
    finally:
        mp.undo()
    assert out["recency"]["pass"] is None
    assert "Read failure, not staleness" in out["recency"]["detail"]


def test_unmeasurable_recency_makes_the_lane_a_question_not_a_pass():
    """The lane must not read PASS off a readable count while its freshness
    column is unmeasured — that is how a board renders a confident green over
    an unmeasured layer."""
    m = _mod()
    mp = pytest.MonkeyPatch()
    try:
        out = _run(mp, _spec(), {"total": 55064, "columns": {},
                                 "freshness": (None, 0, 0)})
    finally:
        mp.undo()
    assert m._lane_verdict(list(out.values())) == "?"


# ── 3. an unknown count is never rendered 0 ─────────────────────────────────
def test_unknown_count_never_renders_zero():
    m = _mod()
    assert m._fmt(None) == "UNKNOWN"
    assert m._fmt(0) == "0"          # a real zero stays a number
    mp = pytest.MonkeyPatch()
    try:
        out = _run(mp, _spec(), {
            "total": None, "count_raises": True,
            "columns": {"created_at": "timestamp with time zone"},
            "freshness": (_ago(1), 1, 1), "mode": (2, 1)})
    finally:
        mp.undo()
    assert out["rows"]["pass"] is None
    assert "UNKNOWN" in out["rows"]["detail"]
    assert " 0 " not in out["rows"]["detail"]


# ── 4. zero in 7d is not a failure; the delta never convicts ────────────────
def test_zero_in_7d_within_cadence_passes():
    """The anti-cry-wolf property. A quarterly layer with nothing new this week
    but a write inside its horizon is HEALTHY."""
    m = _mod()
    mp = pytest.MonkeyPatch()
    try:
        out = _run(mp, _spec(cadence_days=120), {
            "total": 30918,
            "columns": {"created_at": "timestamp with time zone"},
            "freshness": (_ago(3.8), 0, 0), "mode": (4, 30000)})
    finally:
        mp.undo()
    assert out["recency"]["pass"] is True
    assert out["growth"]["pass"] is not False
    assert m._lane_verdict(list(out.values())) == "PASS"


def test_growth_check_never_convicts():
    """Across an exhaustive sweep of delta shapes the growth gauge must never
    return False — only recency may convict."""
    mp = pytest.MonkeyPatch()
    try:
        for d7, d30, total in ((0, 0, 691), (0, 18, 55064),
                               (30000, 30000, 30918), (1366, 2537, 16742),
                               (16, 68, 126858)):
            out = _run(mp, _spec(), {
                "total": total,
                "columns": {"created_at": "timestamp with time zone"},
                "freshness": (_ago(1), d7, d30), "mode": (5, total)})
            assert out["growth"]["pass"] is not False, (d7, d30, total)
    finally:
        mp.undo()


# ── 5. a reload is not growth — and a seed is not a reload ──────────────────
def test_rewrite_is_not_reported_as_growth():
    """Live gas_pipelines: 30,000 of 30,918 rows written in the window. A board
    that called that '+30,000 this week' would be lying in the most flattering
    possible direction."""
    mp = pytest.MonkeyPatch()
    try:
        out = _run(mp, _spec(), {
            "total": LIVE_GAS_ROWS,
            "columns": {"created_at": "timestamp with time zone"},
            "freshness": (_ago(3.8), LIVE_GAS_DELTA, LIVE_GAS_DELTA),
            "mode": (4, LIVE_GAS_DELTA)})
    finally:
        mp.undo()
    d = out["growth"]["detail"]
    assert "MODE=REWRITE" in d
    assert "NOT new assets" in d


def test_seed_load_is_not_a_rewrite():
    """★ REGRESSION. The first draft classified on share-of-busiest-day and
    called `facilities` — 1,366 genuinely new sites in 7d across 103 write-days
    — a truncate-and-reload, because its busiest day is an old SEED holding 68%
    of rows. Mode must key off the WINDOW DELTA, not the busiest day."""
    mp = pytest.MonkeyPatch()
    try:
        out = _run(mp, _spec(entity_key="canonical_slug",
                             count_where="canonical_slug IS NOT NULL",
                             cadence_days=3), {
            "total": LIVE_FACILITIES_DISTINCT,
            "columns": {"first_seen": "timestamp with time zone",
                        "created_at": "timestamp with time zone"},
            "freshness": (_ago(0.2), LIVE_FACILITIES_D7, LIVE_FACILITIES_D30),
            "mode": (LIVE_FACILITIES_WRITE_DAYS, LIVE_FACILITIES_BUSIEST)})
    finally:
        mp.undo()
    d = out["growth"]["detail"]
    assert "MODE=INCREMENTAL" in d, "a seed load must not read as a reload"
    assert "TRUNCATES AND RELOADS" not in d
    assert "real growth" in d


def test_no_writes_in_window_is_its_own_mode():
    mp = pytest.MonkeyPatch()
    try:
        out = _run(mp, _spec(), {
            "total": 691,
            "columns": {"created_at": "timestamp with time zone"},
            "freshness": (_ago(132.9), 0, 0), "mode": (1, 691)})
    finally:
        mp.undo()
    assert "MODE=NO WRITES IN 30d" in out["growth"]["detail"]


# ── 6. the published population is BUILT FROM the executed one ─────────────
def test_population_is_built_from_the_executed_layer_list():
    """PR #2253's rule. Mutating _LAYERS must move the published population —
    a hand-typed list would not follow, which is the drift this catches."""
    m = _mod()
    before = m._population()
    assert before["layers_measured"] == [s["key"] for s in m._LAYERS]
    assert len(before["sql"]) == len(m._LAYERS)

    extra = dict(key="zz_probe", label="probe", table="zz_probe_table",
                 ts_candidates=("created_at",), cadence_days=1,
                 cadence="probe", source="probe")
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(m, "_LAYERS", m._LAYERS + (extra,))
        after = m._population()
    finally:
        mp.undo()
    assert "zz_probe" in after["layers_measured"]
    assert len(after["sql"]) == len(before["sql"]) + 1
    assert any("zz_probe_table" in r["count"] for r in after["sql"])
    # ...and the mutation is fully undone, so the published list is live state.
    assert m._population()["layers_measured"] == before["layers_measured"]


def test_population_publishes_the_sql_that_actually_runs():
    """The published count SQL must be the same string the lane executes —
    built by the same helper, not transcribed alongside it."""
    m = _mod()
    pop = {r["layer"]: r for r in m._population()["sql"]}
    for spec in m._LAYERS:
        assert pop[spec["key"]]["count"] == m._count_sql(spec)
        assert pop[spec["key"]]["cadence_days"] == spec["cadence_days"]


def test_facilities_count_mirrors_canon_exactly():
    """public_endpoints.py counts facilities as COUNT(DISTINCT canonical_slug)
    over discovered_facilities. Counting rows would publish 24,472 against a
    canon of 16,742 and invent a discrepancy."""
    m = _mod()
    spec = next(s for s in m._LAYERS if s["key"] == "facilities")
    sql = m._count_sql(spec)
    assert "COUNT(DISTINCT canonical_slug)" in sql
    assert "FROM discovered_facilities" in sql
    assert "canonical_slug IS NOT NULL" in sql


def test_every_layer_declares_a_cadence_and_a_source():
    """A layer that cannot state what cadence it is judged against is not
    constructible — the reader must be able to disagree with the threshold."""
    for spec in _mod()._LAYERS:
        assert spec["cadence_days"] > 0
        assert spec["cadence"] and spec["source"]
        assert spec["ts_candidates"]


def test_shell_reads_the_populated_subsea_table_not_the_empty_twin():
    """cable_landing_points is live-empty (0 rows); subsea_landing_points holds
    1,908. Pointing a freshness lane at the abandoned twin would render the
    layer permanently unmeasurable for the wrong reason."""
    tables = {s["table"] for s in _mod()._LAYERS}
    assert "subsea_landing_points" in tables
    assert "cable_landing_points" not in tables


# ── 7. no literal % anywhere (psycopg2 percent-substitution trap) ───────────
def test_no_literal_percent_in_any_generated_sql():
    """Every statement runs with NO bound parameters. A literal % would make
    psycopg2 raise the moment a parameter is ever added — and this codebase has
    500'd on exactly that."""
    m = _mod()
    for spec in m._LAYERS:
        for sql in (m._count_sql(spec),
                    m._freshness_sql(spec, "c", "c"),
                    m._mode_sql(spec, "c", "c")):
            assert "%" not in sql, f"{spec['key']}: {sql}"
