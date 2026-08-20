"""Guard: a fail-soft caller must publish WHY it has nothing to say.

#2956 fixed one instance of this (customer_white_glove._measure swallowed a
query exception, returned [], and the customer board served "0 payers ·
systemic_activation_failure false" over 20 real paying customers). #2958 then
fixed the literal-percent TRIGGER at six more call sites but deliberately left
the AMPLIFIER at each — the handler that turns the exception into a
benign-looking number. This file guards the five amplifiers.

The shared claim, in one sentence: **an unmeasured thing must never render as
a measured zero.** Each test below therefore has to pass in BOTH directions —
a genuinely empty result must still read as measured, or the flag is noise
that operators learn to ignore.

  1. osm_crawler       a failed INSERT is not a duplicate POI
  2. linkedin_quad     a fail-OPEN claim is not a won claim
  3. market_brief      a market whose query broke is not a thin market
  4. iso_snapshot      a pipeline that would not load is not zero projects,
                       and a footprint that would not load is not zero
                       facilities — plus the third state the facilities half
                       has and the pipeline half does not: an ISO mapped to no
                       states IS measured, and must keep reading that way
  5. intelligence      a scan that did not run is not a quiet news day

DB-free: fake connections/cursors only. Never imports main (pre-merge CI has
no DB), which is why the intelligence_engine half reads its function out of
the source with ast — that module calls init_intelligence_db() at import.
"""
import ast
import contextlib
import datetime
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from flask import Flask  # noqa: E402


# ── shared fakes ─────────────────────────────────────────────────────

class FakeCursor:
    """Answers by SQL substring. `fail_on` substrings raise instead."""

    def __init__(self, answers=None, fail_on=(), exc=None):
        self.answers = answers or {}
        self.fail_on = tuple(fail_on)
        self.exc = exc or RuntimeError("tuple index out of range")
        self.executed = []
        self._pending = None

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        for frag in self.fail_on:
            if frag in sql:
                raise self.exc
        self._pending = None
        for frag, rows in self.answers.items():
            if frag in sql:
                self._pending = rows
                break

    def fetchone(self):
        rows = self._pending or []
        return rows[0] if rows else None

    def fetchall(self):
        return list(self._pending or [])

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False

    def cursor(self, *a, **k):
        return self._cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    def close(self):
        pass


def _conn_ctx(cursor):
    @contextlib.contextmanager
    def _ctx():
        yield FakeConn(cursor)
    return _ctx


# ═════════════════════════════════════════════════════════════════════
# 1. osm_crawler — inserted / duplicate / FAILED
# ═════════════════════════════════════════════════════════════════════

oc = pytest.importorskip("routes.osm_crawler")

ROW = {"name": "Acme DC Frankfurt", "city": "Frankfurt", "country": "DE",
       "provider": "Acme", "state": None, "address": None,
       "_osm_lat": 50.1, "_osm_lon": 8.6}


def test_insert_row_reports_a_dedup_hit_as_duplicate():
    cur = FakeCursor(answers={"SELECT 1 FROM facilities": [(1,)]})
    outcome, _sid = oc._insert_row(cur, dict(ROW))
    assert outcome == oc.DUPLICATE


def test_insert_row_reports_a_landed_row_as_inserted():
    cur = FakeCursor(answers={"RETURNING id": [(4242,)]})
    outcome, _sid = oc._insert_row(cur, dict(ROW))
    assert outcome == oc.INSERTED


def test_a_failed_insert_is_not_reported_as_a_duplicate():
    """★ THE REGRESSION. _insert_row returned False for both "we already have
    it" and "the INSERT raised", so a data centre we had never seen was
    reported as one we already held."""
    cur = FakeCursor(fail_on=("INSERT INTO facilities",))
    outcome, _sid = oc._insert_row(cur, dict(ROW))
    assert outcome == oc.FAILED, (
        f"a raising INSERT returned {outcome!r} — if that is DUPLICATE, every "
        f"failed insert of a NEW facility is counted as one we already had")


def test_a_failed_dedup_query_is_not_reported_as_a_duplicate():
    """The dedup query raising means we never learned whether it is a
    duplicate. That is the absence of a measurement, not a hit."""
    cur = FakeCursor(fail_on=("SELECT 1 FROM facilities",))
    outcome, _sid = oc._insert_row(cur, dict(ROW))
    assert outcome == oc.FAILED


def test_an_insert_that_returns_no_id_is_a_failure():
    """No ON CONFLICT on that INSERT, so "no row back" cannot mean a
    conflict — it means the write did not land."""
    cur = FakeCursor()  # RETURNING id yields nothing
    outcome, _sid = oc._insert_row(cur, dict(ROW))
    assert outcome == oc.FAILED


def _crawl_with(monkeypatch, outcome):
    """Drive _crawl over exactly one POI whose insert returns `outcome`."""
    monkeypatch.setattr(oc, "ENABLED", True)
    monkeypatch.setattr(oc, "BBOXES", {"testland": (0.0, 0.0, 1.0, 1.0)})
    monkeypatch.setattr(oc, "_query_bbox", lambda bbox: ([{"id": 1}], "ok"))
    monkeypatch.setattr(oc, "_osm_to_row", lambda e, slug: dict(ROW))
    monkeypatch.setattr(oc, "_insert_row", lambda cur, r: (outcome, "osm_x"))
    monkeypatch.setattr(oc, "_ensure_log_table", lambda: None)
    monkeypatch.setattr(oc.time, "sleep", lambda *_a, **_k: None)
    # One connection for the row work; None afterwards so the run log and the
    # sentinels no-op instead of reaching a DB.
    conns = [FakeConn(FakeCursor())]
    monkeypatch.setattr(oc, "_get_db", lambda: conns.pop() if conns else None)
    return oc._crawl("testland", dry_run=False)


def test_crawl_counts_a_failed_insert_as_an_error_not_a_dup(monkeypatch):
    """★ THE AMPLIFIER. summary["pois_dup"] += 1 was the `else` of `if added`,
    so the run summary and osm_crawl_log reported a broken crawl as a normal
    one that happened to find nothing new."""
    s = _crawl_with(monkeypatch, oc.FAILED)
    assert s["pois_dup"] == 0, (
        "a failed insert still lands in pois_dup — the run reads as 'we "
        "already had it'")
    assert s["errors"] == 1
    assert s["insert_failed"] == 1
    assert s["insert_failures"], "the failure was counted but not explained"
    assert s["insert_failures"][0]["name"].startswith("Acme DC")


def test_crawl_still_counts_a_real_duplicate_as_a_duplicate(monkeypatch):
    """The other direction: if everything became an error, pois_dup would be
    dead and the flag would carry no information."""
    s = _crawl_with(monkeypatch, oc.DUPLICATE)
    assert s["pois_dup"] == 1
    assert s["errors"] == 0
    assert s["insert_failed"] == 0


def test_crawl_still_counts_a_real_insert_as_new(monkeypatch):
    s = _crawl_with(monkeypatch, oc.INSERTED)
    assert s["pois_new"] == 1
    assert s["pois_dup"] == 0 and s["errors"] == 0


# ═════════════════════════════════════════════════════════════════════
# 2. linkedin_quad — claimed vs could-not-check
# ═════════════════════════════════════════════════════════════════════

lq = pytest.importorskip("routes.linkedin_quad_daily")

SLOT_DATE = datetime.date(2026, 7, 17)


@pytest.fixture(autouse=True)
def _reset_claim_guard():
    before = dict(lq._CLAIM_GUARD)
    yield
    lq._CLAIM_GUARD.clear()
    lq._CLAIM_GUARD.update(before)


def test_a_failed_claim_still_fails_open(monkeypatch):
    """Non-negotiable: the guard must never dark-hold the feed."""
    cur = FakeCursor(fail_on=("INSERT INTO linkedin_quad_posts",))
    monkeypatch.setattr(lq, "_pg", object())
    monkeypatch.setattr(lq, "_conn", _conn_ctx(cur))
    monkeypatch.setattr(lq, "_dsn", lambda: "postgres://x")
    assert lq._claim_slot(SLOT_DATE, 8, "dcpi_mover", "data") is True


def test_a_failed_claim_is_recorded_as_unverified(monkeypatch):
    """★ THE REGRESSION. `except Exception: return True` made "I claimed it"
    and "I could not check" the same answer, and the 2026-07-17 double-post
    guard was fully disabled for three days with no signal anywhere."""
    cur = FakeCursor(fail_on=("INSERT INTO linkedin_quad_posts",))
    monkeypatch.setattr(lq, "_pg", object())
    monkeypatch.setattr(lq, "_conn", _conn_ctx(cur))
    monkeypatch.setattr(lq, "_dsn", lambda: "postgres://x")
    lq._claim_slot(SLOT_DATE, 8, "dcpi_mover", "data")
    assert lq._CLAIM_GUARD["verified"] is False, (
        "a claim that threw is published as verified — indistinguishable "
        "from a claim that was actually won")
    assert lq._CLAIM_GUARD["reason"], "the failure has no reason attached"
    assert lq._CLAIM_GUARD["at"] and lq._CLAIM_GUARD["slot"]


def test_a_won_claim_is_verified(monkeypatch):
    """The other direction — a flag stuck on `unverified` is the same lie
    pointed the other way, and it trains the operator to ignore it."""
    cur = FakeCursor(answers={"INSERT INTO linkedin_quad_posts": [(1,)]})
    monkeypatch.setattr(lq, "_pg", object())
    monkeypatch.setattr(lq, "_conn", _conn_ctx(cur))
    monkeypatch.setattr(lq, "_dsn", lambda: "postgres://x")
    lq._CLAIM_GUARD.update({"verified": False, "reason": "stale"})
    assert lq._claim_slot(SLOT_DATE, 8, "dcpi_mover", "data") is True
    assert lq._CLAIM_GUARD["verified"] is True
    assert lq._CLAIM_GUARD["reason"] is None


def test_a_lost_claim_is_also_verified(monkeypatch):
    """Losing the race IS a measurement — the guard worked."""
    cur = FakeCursor()  # RETURNING yields nothing: the peer holds the slot
    monkeypatch.setattr(lq, "_pg", object())
    monkeypatch.setattr(lq, "_conn", _conn_ctx(cur))
    monkeypatch.setattr(lq, "_dsn", lambda: "postgres://x")
    assert lq._claim_slot(SLOT_DATE, 8, "dcpi_mover", "data") is False
    assert lq._CLAIM_GUARD["verified"] is True


def test_no_database_is_unverified_not_claimed(monkeypatch):
    monkeypatch.setattr(lq, "_dsn", lambda: "")
    assert lq._claim_slot(SLOT_DATE, 8, "dcpi_mover", "data") is True
    assert lq._CLAIM_GUARD["verified"] is False


def test_the_unverified_stamp_query_is_percent_free(monkeypatch):
    """The claim query died on a literal percent inside the comment that
    explains the percent rule. The stamp that reports that failure must not
    be able to die the same way — it runs with an args tuple."""
    cur = FakeCursor()
    monkeypatch.setattr(lq, "_pg", object())
    monkeypatch.setattr(lq, "_conn", _conn_ctx(cur))
    monkeypatch.setattr(lq, "_dsn", lambda: "postgres://x")
    lq._stamp_claim_unverified(SLOT_DATE, 8, "dcpi_mover", "data", "boom")
    sql, params = cur.executed[0]
    assert params is not None, "no args tuple — then this guard is moot"
    for i, ch in enumerate(sql):
        if ch != "%":
            continue
        nxt = sql[i + 1:i + 2]
        assert nxt in ("%", "s", "("), (
            f"bare percent at offset {i} in the unverified-claim stamp: "
            f"...{sql[max(0, i - 40):i + 20]}...")


def test_status_publishes_whether_the_claim_guard_ran(monkeypatch):
    """A guard nobody can see the state of is a guard nobody can trust."""
    monkeypatch.setattr(lq, "_dsn", lambda: "")   # skip the DB block
    lq._CLAIM_GUARD.update({"verified": False, "reason": "IndexError: x",
                            "at": "2026-07-17T08:00:00Z", "slot": "…"})
    app = Flask(__name__)
    app.register_blueprint(lq.linkedin_quad_bp)
    body = app.test_client().get("/api/v1/linkedin-quad/status").get_json()
    assert body["claim_guard"]["verified"] is False
    assert body["claim_guard"]["reason"] == "IndexError: x"


# ═════════════════════════════════════════════════════════════════════
# 3. market_brief — unmeasured is not thin
# ═════════════════════════════════════════════════════════════════════

mb = pytest.importorskip("routes.market_brief")

_MARKETS = [("ashburn", "Ashburn", "VA", "PJM", "BUILD")]


def _matrix(monkeypatch, fail_on=()):
    cur = FakeCursor(
        answers={
            "FROM market_power_scores": _MARKETS,
            "COUNT(*) FILTER": [(11, 4)],
            "COUNT(DISTINCT provider)": [(3,)],
            "FROM deals": [(2,)],
            "FROM water_risk": [(0.5, 2)],
        },
        fail_on=fail_on)
    monkeypatch.setattr(mb, "_admin_authorized", lambda: True)
    monkeypatch.setattr(mb, "_conn", lambda: FakeConn(cur))
    app = Flask(__name__)
    app.register_blueprint(mb.market_brief_bp)
    return app.test_client().get(
        "/api/v1/admin/market-coverage-matrix").get_json()


def test_a_market_whose_counts_broke_is_not_published_as_thin(monkeypatch):
    """★ THE REGRESSION. `except: facilities_n, pipeline_n = 0, 0` rendered a
    market the queries could not read as the WORST-covered market on the
    board — and this endpoint exists to point operators at exactly that."""
    body = _matrix(monkeypatch, fail_on=("COUNT(*) FILTER",))
    m = body["markets"][0]
    assert m["measured"] is False, "a market whose query threw reads as measured"
    assert m["tier"] == "unmeasured", (
        f"tier {m['tier']!r} — a broken market is being graded on the same "
        f"scale as a real one")
    assert m["facilities_n"] is None and m["pipeline_n"] is None, (
        "a count whose query failed is published as 0, which is a claim")
    assert "facilities" in m["unmeasured_lanes"]
    assert m["errors"] and m["errors"]["facilities"]
    assert body["summary"]["unmeasured"] == 1
    assert body["summary"]["thin"] == 0, "counted as thin AND as unmeasured"


def test_a_genuinely_covered_market_still_reads_as_measured(monkeypatch):
    """The other direction, or `measured` is a constant."""
    body = _matrix(monkeypatch)
    m = body["markets"][0]
    assert m["measured"] is True
    assert m["tier"] in ("full", "medium", "thin")
    assert m["facilities_n"] == 11 and m["pipeline_n"] == 4
    assert m["unmeasured_lanes"] == [] and m["errors"] is None
    assert body["summary"]["unmeasured"] == 0
    assert body["summary"]["measured_markets"] == 1


def test_one_broken_lane_only_nulls_its_own_counts(monkeypatch):
    """Precision matters: blanking every number because one lane failed would
    throw away the coverage we DID measure."""
    body = _matrix(monkeypatch, fail_on=("COUNT(DISTINCT provider)",))
    m = body["markets"][0]
    assert m["operators_n"] is None
    assert m["facilities_n"] == 11, "an unrelated lane was nulled too"
    assert m["unmeasured_lanes"] == ["operators"]


# ═════════════════════════════════════════════════════════════════════
# 4. iso_snapshot — no pipeline block is not zero projects
# ═════════════════════════════════════════════════════════════════════

iso = pytest.importorskip("routes.iso_snapshot")


def test_pipeline_returns_a_reason_when_it_cannot_load():
    """★ THE REGRESSION. `except: return None` — the snapshot omitted the
    block and /iso/comparison published pipeline_projects: 0 for every ISO."""
    cur = FakeCursor(fail_on=("FROM capacity_pipeline",))
    block, err = iso._pipeline_for_iso(cur, "PJM")
    assert block is None
    assert err and err["reason"], "the failure is still silent"
    assert err["at"]


def test_pipeline_error_names_which_side_of_the_socket_failed():
    """The 07-31 docstring blamed Postgres for a failure that never reached
    Postgres. `kind` is what stops that diagnosis going stale again."""
    import psycopg2

    client = FakeCursor(fail_on=("FROM capacity_pipeline",),
                        exc=IndexError("tuple index out of range"))
    _b, err = iso._pipeline_for_iso(client, "PJM")
    assert err["kind"] == "client_side", (
        "an IndexError raised by psycopg2's own parameter interpolation was "
        "labelled a database failure — that mislabel survived three weeks")

    server = FakeCursor(fail_on=("FROM capacity_pipeline",),
                        exc=psycopg2.errors.UndefinedColumn(
                            'column "iso" does not exist'))
    _b, err = iso._pipeline_for_iso(server, "PJM")
    assert err["kind"] == "database"


def test_a_real_rollup_reports_no_error():
    """Other direction."""
    cur = FakeCursor(answers={"FROM capacity_pipeline": [(7, 1234.5, 2, 400.0)]})
    block, err = iso._pipeline_for_iso(cur, "PJM")
    assert err is None
    assert block["project_count"] == 7 and block["under_construction_mw"] == 400.0


def test_an_unmeasured_pipeline_is_not_zero_projects():
    """The comparison table's `.get("project_count", 0)` was the loudest form
    of the lie — ten ISOs each publishing a confident 0."""
    src = open(os.path.join(ROOT, "routes", "iso_snapshot.py"),
               encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "iso_comparison")
    keys = [k.value for n in ast.walk(fn) if isinstance(n, ast.Dict)
            for k in n.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)]
    assert "pipeline_measured" in keys, (
        "the comparison row publishes pipeline_projects with no way to tell a "
        "measured 0 from a failed rollup")


def test_the_docstring_no_longer_asserts_the_unmeasured_cause():
    """The 2026-07-31 note stated the failure was an UndefinedColumn from
    Postgres. Measured 2026-08-19: on main it is an IndexError raised
    client-side, before anything is sent. A wrong root cause in a docstring
    outlives the code it describes — pin the correction."""
    doc = iso._pipeline_for_iso.__doc__ or ""
    assert "IndexError" in doc and "RE-MEASURED" in doc, (
        "the corrected diagnosis was dropped from the docstring")


# ── 4b. the same amplifier on the facilities half ────────────────────
# _facilities_for_iso had TWO bare `except: ... return None` blocks, and
# /iso/comparison rendered the result as `"facility_count": 0`. It also has a
# state the pipeline rollup does not: it returned None both when a query
# RAISED and when the ISO->state mapping came back genuinely empty.

PJM_STATES = {"FROM market_power_scores": [("PA",), ("OH",)]}


def _iso_dict_keys(fn_name):
    """String keys of every dict literal inside a top-level function."""
    src = open(os.path.join(ROOT, "routes", "iso_snapshot.py"),
               encoding="utf-8").read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == fn_name)
    return [k.value for n in ast.walk(fn) if isinstance(n, ast.Dict)
            for k in n.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)]


def test_facilities_returns_a_reason_when_the_footprint_cannot_load():
    """★ THE REGRESSION, sibling edition. The COUNT/SUM threw, the function
    returned a bare None, and the head-to-head table published a confident
    "0 facilities" — which reads as a market with nothing built in it."""
    cur = FakeCursor(answers=PJM_STATES, fail_on=("FROM facilities",))
    block, err = iso._facilities_for_iso(cur, "PJM")
    assert block is None
    assert err and err["reason"], "the failure is still silent"
    assert err["at"]


def test_a_broken_state_lookup_is_not_an_iso_with_no_states():
    """★ THE MIDDLE CASE. The old `except: states = []` fell straight into
    `if not states: return None`, so a broken market_power_scores query was
    indistinguishable from an ISO that genuinely maps to no states. One is
    unmeasured; the other is a measurement."""
    cur = FakeCursor(fail_on=("FROM market_power_scores",))
    block, err = iso._facilities_for_iso(cur, "PJM")
    assert block is None, "a raising state lookup still produced a block"
    assert err and err["reason"], (
        "the state lookup's exception is still swallowed into an empty "
        "state set — the failure that started this")


def test_an_iso_with_no_mapped_states_is_not_a_count_of_zero():
    """★★ THE BPA CASE, measured live in production 2026-08-20 — and the
    correction to this file's own first answer.

    #2962 called this path a "measured empty" and returned facility_count 0,
    on the reasoning that `states: []` in the same block kept the zero honest.
    It did not. BPA is absent from market_power_scores entirely
    (/api/v1/dcpi/iso/BPA -> iso_not_found), so it took this path and
    /iso/comparison published `facility_count: 0, facilities_measured: true`
    over territory holding Quincy, Hillsboro and Umatilla — a STRONGER claim
    than the bare 0 it replaced, because the flag asserted we had looked.

    Nothing raised, so it is still not an `error`. But nothing was measured
    either, and a None cannot be misread as a count."""
    cur = FakeCursor(answers={"FROM market_power_scores": []})
    block, err = iso._facilities_for_iso(cur, "BPA")
    assert err is None, "nothing raised here — this is not a failure"
    assert block is not None
    assert block["facility_count"] is None, (
        "an ISO with no row in market_power_scores published a confident 0 "
        "facilities")
    assert block["total_facility_mw"] is None
    assert block["basis"] == "no_iso_state_mapping"
    assert block["states"] == []


def test_a_genuine_zero_over_a_real_state_set_stays_zero():
    """The over-correction guard. An ISO whose states DO resolve and which
    genuinely has no facilities must keep reporting 0 — if the fix above
    turned every zero into None, the endpoint would just be unreadable in a
    new direction."""
    cur = FakeCursor(answers={**PJM_STATES, "FROM facilities": [(0, 0)]})
    block, err = iso._facilities_for_iso(cur, "PJM")
    assert err is None
    assert block["facility_count"] == 0, "a real measured zero was erased"
    assert block["basis"] == "iso_state_footprint"


def test_measured_flags_require_a_count_not_just_the_absence_of_an_error():
    """Both callers computed `facilities_measured` as `err is None`, which is
    exactly what called BPA measured. The flag has to depend on the count."""
    src = open(os.path.join(ROOT, "routes", "iso_snapshot.py"),
               encoding="utf-8").read()
    tree = ast.parse(src)
    checked = 0
    for fn_name in ("iso_comparison", "iso_snapshot"):
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == fn_name)
        for d in ast.walk(fn):
            if not isinstance(d, ast.Dict):
                continue
            for k, v in zip(d.keys, d.values):
                if (isinstance(k, ast.Constant)
                        and k.value == "facilities_measured"):
                    seg = ast.get_source_segment(src, v) or ""
                    assert "facility_count" in seg, (
                        f"{fn_name} marks facilities measured from the absence "
                        f"of an error alone: {seg!r}")
                    checked += 1
    assert checked == 2, (
        f"expected both callers to publish facilities_measured, found {checked}")


def test_intl_rows_do_not_publish_a_zero_they_never_measured():
    """The last unflagged zeros on this endpoint. The intl modules are
    baseline power-market models carrying LMP/carbon/renewables and NO
    facility or pipeline inventory, so `facility_count: 0` asserted that
    Hydro-Québec and the Nordics have no data centres — in the same table
    where the US rows publish real counts."""
    src = open(os.path.join(ROOT, "routes", "iso_snapshot.py"),
               encoding="utf-8").read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef)
              and n.name == "_intl_snapshot_row")
    pairs = {}
    for d in ast.walk(fn):
        if not isinstance(d, ast.Dict):
            continue
        for k, v in zip(d.keys, d.values):
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                pairs[k.value] = v
    for field in ("facility_count", "pipeline_projects"):
        v = pairs.get(field)
        assert isinstance(v, ast.Constant) and v.value is None, (
            f"intl rows still publish a literal {field} they never measured")
    for flag, want in (("facilities_measured", False),
                       ("pipeline_measured", False)):
        v = pairs.get(flag)
        assert isinstance(v, ast.Constant) and v.value is want, (
            f"intl rows omit {flag}, so a reader cannot tell their blanks "
            f"from a US row's real measurement")
    assert "facilities_basis" in pairs and "pipeline_basis" in pairs


def test_facilities_error_names_which_side_of_the_socket_failed():
    """Same two failure modes, same reason to keep them apart — a wrong root
    cause outlives the code it describes. See _pipeline_for_iso's docstring."""
    import psycopg2

    client = FakeCursor(answers=PJM_STATES, fail_on=("FROM facilities",),
                        exc=IndexError("tuple index out of range"))
    _b, err = iso._facilities_for_iso(client, "PJM")
    assert err["kind"] == "client_side", (
        "a failure raised on THIS side of the socket was labelled a database "
        "failure — the mislabel that survived three weeks on the sibling")

    server = FakeCursor(answers=PJM_STATES, fail_on=("FROM facilities",),
                        exc=psycopg2.errors.UndefinedColumn(
                            'column "power_mw" does not exist'))
    _b, err = iso._facilities_for_iso(server, "PJM")
    assert err["kind"] == "database"


def test_a_real_footprint_reports_no_error():
    """Other direction."""
    cur = FakeCursor(answers={**PJM_STATES, "FROM facilities": [(42, 1234.5)]})
    block, err = iso._facilities_for_iso(cur, "PJM")
    assert err is None
    assert block["facility_count"] == 42
    assert block["total_facility_mw"] == 1234.5
    assert block["states"] == ["PA", "OH"]


def test_an_unmeasured_footprint_is_not_zero_facilities():
    """The comparison table's `.get("facility_count", 0)` — the same default
    as pipeline_projects, over the number a reader trusts most."""
    keys = _iso_dict_keys("iso_comparison")
    assert "facilities_measured" in keys, (
        "the comparison row publishes facility_count with no way to tell a "
        "measured 0 from a footprint that could not be loaded")


def test_the_snapshot_says_when_it_could_not_produce_a_footprint():
    """The snapshot route omitted the block entirely and said nothing about
    it — indistinguishable from an ISO we hold no facilities for."""
    keys = _iso_dict_keys("iso_snapshot")
    assert "facilities_measured" in keys and "facilities_unavailable" in keys, (
        "the snapshot drops the facilities block with no statement that it "
        "could not be produced")


# ── 4c. one failed query must not zero every LATER read ──────────────
# ★★ Measured in production 2026-08-20, minutes after 4b's reason channels
# went live and using them as the instrument. /api/v1/iso/comparison runs all
# 13 ISOs over ONE connection. ERCOT's pipeline query hit a real
# UndefinedColumn; that aborted the transaction, and all 25 remaining reads
# (ERCOT's own facilities lookup + both blocks for the other 12 ISOs) came
# back InFailedSqlTransaction. Every one of the 26 had been rendering as a
# confident 0. Same class as the all-zero /agent/index.


class _Poisoned(Exception):
    """Stand-in for psycopg2.errors.InFailedSqlTransaction."""


class PoisonableCursor(FakeCursor):
    """FakeCursor + real Postgres transaction semantics.

    After ANY failed statement, EVERY later statement on the same connection
    raises until someone rolls back. Without that, a fake makes the cascade
    untestable — each query looks independent, which is exactly the wrong
    mental model and the reason this shipped.
    """

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.poisoned = False
        self.rollbacks = 0
        cur = self

        class _Conn:
            def rollback(self):
                cur.rollbacks += 1
                cur.poisoned = False

        self.connection = _Conn()

    def execute(self, sql, params=None):
        if self.poisoned:
            raise _Poisoned("current transaction is aborted, commands "
                            "ignored until end of transaction block")
        try:
            return super().execute(sql, params)
        except Exception:
            self.poisoned = True
            raise


def test_the_poisonable_cursor_actually_poisons():
    """★ GUARD THE GUARD. Every cascade test below asserts a GOOD outcome, so
    each of them also passes when the fake simply cannot poison — mutation
    testing caught exactly that (neutering the poisoning left them all green).
    Pin the fake's semantics directly, or the cascade suite is decoration."""
    cur = PoisonableCursor(fail_on=("FROM capacity_pipeline",))

    with pytest.raises(Exception):
        cur.execute("SELECT 1 FROM capacity_pipeline")
    assert cur.poisoned, "a failed statement did not abort the transaction"

    with pytest.raises(_Poisoned):
        # An UNRELATED, perfectly good query must now die too — that is the
        # whole mechanism, and what produced 25 collateral zeros.
        cur.execute("SELECT count(*) FROM facilities")

    cur.connection.rollback()
    assert not cur.poisoned
    cur.execute("SELECT count(*) FROM facilities")  # recovers, no raise


def test_a_failed_state_lookup_rolls_the_transaction_back():
    """The market_power_scores lookup is a THIRD failure path, and mutation
    testing showed it was uncovered: the cascade tests reach it only after an
    earlier rollback has already cleaned up, so dropping its `cur` argument
    changed nothing they measured."""
    cur = PoisonableCursor(fail_on=("FROM market_power_scores",))
    _b, err = iso._facilities_for_iso(cur, "PJM")
    assert err is not None
    assert cur.rollbacks == 1, (
        "a failed state lookup left the transaction poisoned, so the next "
        "ISO's reads die on a connection this one broke")
    assert not cur.poisoned


def test_a_failed_pipeline_query_rolls_the_transaction_back():
    cur = PoisonableCursor(fail_on=("FROM capacity_pipeline",))
    _b, err = iso._pipeline_for_iso(cur, "ERCOT")
    assert err is not None
    assert cur.rollbacks == 1, "the aborted transaction was left poisoned"
    assert not cur.poisoned


def test_a_failed_facilities_query_rolls_the_transaction_back():
    cur = PoisonableCursor(answers=PJM_STATES, fail_on=("FROM facilities",))
    _b, err = iso._facilities_for_iso(cur, "PJM")
    assert err is not None
    assert cur.rollbacks == 1, "the aborted transaction was left poisoned"
    assert not cur.poisoned


def test_a_broken_pipeline_does_not_zero_the_facilities_read():
    """★ THE PRODUCTION REGRESSION, minimal form. ERCOT's pipeline rollup
    genuinely fails; its facilities footprint, on the SAME connection, is a
    perfectly good query and must still be measured. In production it was not
    — it returned InFailedSqlTransaction and published 0 facilities."""
    cur = PoisonableCursor(
        answers={**PJM_STATES, "FROM facilities": [(42, 1234.5)]},
        fail_on=("FROM capacity_pipeline",))

    _pb, perr = iso._pipeline_for_iso(cur, "ERCOT")
    assert perr is not None, "the pipeline failure is the premise of this test"

    fblock, ferr = iso._facilities_for_iso(cur, "ERCOT")
    assert ferr is None, (
        "one broken query poisoned the transaction and took an unrelated, "
        "working read down with it — 26 dead reads in production, every one "
        "of them published as a confident 0")
    assert fblock["facility_count"] == 42


def test_one_isos_failure_does_not_zero_the_next_iso():
    """The cascade across the loop: /iso/comparison shares one connection over
    all 13 ISOs, so ISO #1's failure used to zero ISOs #2..#13 as well."""
    cur = PoisonableCursor(
        answers={**PJM_STATES, "FROM facilities": [(7, 100.0)]},
        fail_on=("FROM capacity_pipeline",))

    iso._pipeline_for_iso(cur, "ERCOT")          # ISO #1 fails for real
    block, err = iso._facilities_for_iso(cur, "CAISO")   # ISO #2, same conn
    assert err is None and block["facility_count"] == 7, (
        "a later ISO in the same loop was still collateral damage")


def test_rollback_failure_still_returns_the_original_error():
    """Cleanup must never replace the diagnosis. If rollback itself throws,
    the caller still needs the error that actually broke the query."""
    cur = PoisonableCursor(fail_on=("FROM capacity_pipeline",),
                           exc=IndexError("tuple index out of range"))

    class _Boom:
        def rollback(self):
            raise RuntimeError("connection already closed")

    cur.connection = _Boom()
    block, err = iso._pipeline_for_iso(cur, "ERCOT")
    assert block is None
    # The discriminator has to be the ORIGINAL exception's identity, not just
    # "some error came back" — a cleanup failure that overwrote the diagnosis
    # would still satisfy a weaker assertion.
    assert err["reason"].startswith("IndexError"), (
        f"a secondary cleanup failure masked the real cause: {err['reason']}")
    assert "connection already closed" not in err["reason"]
    assert err["kind"] == "client_side", (
        "the cleanup exception also rewrote which side of the socket failed")


# ═════════════════════════════════════════════════════════════════════
# 5. intelligence_engine — a scan that did not run
# ═════════════════════════════════════════════════════════════════════
# Read, not imported: intelligence_engine calls init_intelligence_db() at
# module scope, which reaches main.py and a DB pool.

IE = os.path.join(ROOT, "intelligence_engine.py")


def _load_deal_scan(db_factory):
    """Exec check_for_new_deals + _scan_lanes_ok + _DEAL_SCAN_ERRORS against
    stubs, the way this repo's other source-level tests do."""
    with open(IE, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=IE)
    wanted = {"check_for_new_deals", "_scan_lanes_ok"}
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            nodes.append(node)
        elif (isinstance(node, ast.Assign)
              and any(getattr(t, "id", None) == "_DEAL_SCAN_ERRORS"
                      for t in node.targets)):
            nodes.append(node)
    assert len(nodes) == 3, (
        f"expected _DEAL_SCAN_ERRORS + both functions in {IE}, found "
        f"{[getattr(n, 'name', '_DEAL_SCAN_ERRORS') for n in nodes]}")
    ns = {"get_db": db_factory, "datetime": datetime.datetime,
          "List": list, "Dict": dict}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), IE, "exec"), ns)  # noqa: S102
    return ns


def _db(fail_on=()):
    def _factory():
        return FakeConn(FakeCursor(
            answers={"FROM capacity_tracking": [],
                     "FROM announcements": []},
            fail_on=fail_on))
    return _factory


def test_a_broken_deal_query_does_not_read_as_a_quiet_news_day():
    """★ THE REGRESSION. Two bare `except: pass` blocks, so
    /api/v1/intelligence/alerts answered {"alerts": [], "count": 0} whether
    the news was quiet or the query had never run once."""
    ns = _load_deal_scan(_db(fail_on=("FROM announcements",)))
    alerts = ns["check_for_new_deals"]()
    assert alerts == []
    assert ns["_scan_lanes_ok"]() is False, (
        "a lane that threw reports as scanned — an empty alert list then "
        "means nothing at all")
    assert ns["_DEAL_SCAN_ERRORS"]["deals"]
    assert ns["_DEAL_SCAN_ERRORS"]["capacity"] is None, (
        "one lane failing marked the other unmeasured too")


def test_a_broken_capacity_lane_does_not_taint_the_deal_lane():
    """The mirror of the case above. Added because a mutation that made the
    capacity handler mark BOTH lanes survived the deal-lane test — that test
    fails the OTHER lane, so it never reached the mutated line. Per-lane
    attribution is the whole point: "which number do I distrust" is the
    question the reader is asking."""
    ns = _load_deal_scan(_db(fail_on=("FROM capacity_tracking",)))
    ns["check_for_new_deals"]()
    assert ns["_DEAL_SCAN_ERRORS"]["capacity"]
    assert ns["_DEAL_SCAN_ERRORS"]["deals"] is None, (
        "the capacity lane's failure was attributed to the deal lane too")
    assert ns["_scan_lanes_ok"]() is False


def test_a_genuinely_quiet_day_still_reads_as_scanned():
    """Other direction — otherwise `scanned` is just False forever."""
    ns = _load_deal_scan(_db())
    assert ns["check_for_new_deals"]() == []
    assert ns["_scan_lanes_ok"]() is True


def test_the_scan_state_is_reset_between_runs():
    """A sticky error would make one bad minute look like a permanent
    outage — the mirror-image lie."""
    ns = _load_deal_scan(_db(fail_on=("FROM announcements",)))
    ns["check_for_new_deals"]()
    assert ns["_scan_lanes_ok"]() is False
    # Same namespace, healthy DB this time.
    ns["get_db"] = _db()
    ns["check_for_new_deals"]()
    assert ns["_scan_lanes_ok"]() is True


MSG = "No new significant deals detected"


def test_the_daily_report_does_not_claim_no_deals_when_it_did_not_scan():
    """run_daily_intelligence printed "No new significant deals detected" on
    the empty list regardless of why it was empty.

    Keyed on the BRANCH CONDITION, not on the message's position in the
    source: a first pass asserted only that `_scan_lanes_ok()` appeared
    somewhere above the message, and it survived replacing the branch with
    `elif True:` — an earlier, unrelated call to the same function was
    holding the assertion up. A guard that passes with the defect live is
    the thing this whole file exists to stop.
    """
    with open(IE, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=IE)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
              and n.name == "run_daily_intelligence")
    owners = [n for n in ast.walk(fn) if isinstance(n, ast.If)
              and any(isinstance(c, ast.Constant) and c.value == MSG
                      for stmt in n.body for c in ast.walk(stmt))]
    assert len(owners) == 1, (
        f"expected exactly one branch to emit {MSG!r}, found {len(owners)} — "
        f"re-point this guard")
    guard = owners[0].test
    names = {n.id for n in ast.walk(guard) if isinstance(n, ast.Name)}
    assert "_scan_lanes_ok" in names, (
        f"the {MSG!r} branch is reachable without checking that the scan ran "
        f"— its condition is {ast.dump(guard)[:120]}")


def test_bare_excepts_are_gone_from_the_deal_scan():
    """`except:` also swallows KeyboardInterrupt/SystemExit, and it is what
    made the reason unrecoverable in the first place."""
    with open(IE, encoding="utf-8") as fh:
        src = fh.read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef)
              and n.name == "check_for_new_deals")
    bare = [h.lineno for h in ast.walk(fn)
            if isinstance(h, ast.ExceptHandler) and h.type is None]
    assert not bare, f"bare except still in check_for_new_deals at {bare}"
