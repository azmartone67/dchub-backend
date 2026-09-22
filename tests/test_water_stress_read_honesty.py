"""Read-honesty fence for the three surfaces that carried the dead water read
after #5259 fixed the first one — 2026-09-22.

THE BUG, measured live against prod Neon on 2026-09-21.
Four route modules asked `usgs_water_stress` for a column it has never had:

    SELECT AVG(stress_index) ... FROM usgs_water_stress WHERE UPPER(state)=%s
    -> UndefinedColumn: column "stress_index" does not exist

That table's real columns are id / site_id / site_name / latitude / longitude
/ state / county / aquifer_name / well_depth_ft / water_level_ft /
water_level_date / site_type / updated_at — 560 rows across 16 states. Its
`water_level_ft` groundwater proxy is NOT a stand-in: it was withdrawn
2026-07-07 for reading INVERTED (`_UNSUPPORTED_OBJECTIVES` in
routes/interconnection_queues.py). The real signal is
`water_risk.water_stress_score`, 0-100, source 'wri_aqueduct', 51 rows.
`baseline_water_stress` is NULL on all 51, so it is never a fallback.

#5259 fixed routes/site_simulator.py. This fence covers the other three:

  routes/dcpi.py  api_score_market_v2   GET /api/v1/dcpi/scores/<slug>/v2
      The water read sat inside `with _conn() as c` — psycopg2's connection
      context manager is a TRANSACTION manager, not a closer, so autocommit
      did not apply. UndefinedColumn aborted the transaction and the
      eia_retail_rates read that FOLLOWED died of InFailedSqlTransaction.
      Both water_stress_index and ppa_rate_cents_kwh served null behind
      HTTP 200, and compute_water_risk_score published its neutral 50.

  routes/dcpi.py  api_dcpi_recommend    GET /api/v1/dcpi/recommend
      Same dead read in `GROUP BY UPPER(state)` form, so `state_water` was
      ALWAYS empty and `water_ok` was vacuously true — water_stress_max
      filtered nothing. Independently, `state_rates` was keyed by
      `UPPER(state)` = "VIRGINIA" (eia_retail_rates stores FULL names) but
      looked up by the 2-letter code off market_power_scores, so every
      lookup missed and max_retail_rate_cents filtered nothing either.

  routes/land_power_mcp.py  _build_analysis
      Same dead water read, plus `UPPER(state) = 'VA'` against
      eia_retail_rates — 0 rows live, while 'VIRGINIA' returned 10
      (industrial 10.09 c/kWh, period 2026). A clean query that answered
      "no data" forever.

WHAT IS PINNED HERE
  * One failing read cannot blank the reads after it — asserted with real
    psycopg2 abort semantics (poison=True), for EVERY read in turn.
  * A read that failed publishes null AND names itself, never 0/[].
  * The dead table/column stay gone from every SQL literal on all three.
  * Both retail reads match the full state name, and the bulk one keys its
    dict by USPS code so the lookup can actually hit.
  * The 1-5 band comes from the ONE shared implementation and is
    direction-correct: arid out-ranks wet.
  * Anti-vacuous floors, per the #2062 lesson: a fence that goes green
    because the thing it inspects became empty is worse than no fence.

House rules: no DB, never import main, nothing at module scope.
Run:  python3 -m pytest tests/test_water_stress_read_honesty.py -v
"""
from __future__ import annotations

import ast
import os
import re

import pytest

import routes.dcpi as dcpi
import routes.land_power_mcp as lpm
from util.water_stress import water_band

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DCPI = "routes/dcpi.py"
LPM = "routes/land_power_mcp.py"
SIM = "routes/site_simulator.py"
ALL_ROUTES = (DCPI, LPM, SIM)


# ---------------------------------------------------------------- fake driver

class FakeConn:
    def __init__(self, cur):
        self._cur = cur
        self.aborted = False
        self.closed = False
        self.autocommit = True

    def rollback(self):
        self.aborted = False
        self._cur.rollbacks += 1

    def cursor(self, **kw):
        return self._cur

    def close(self):
        self.closed = True


class FakeCursor:
    """psycopg2 semantics INSIDE an explicit transaction.

    Once a statement errors the CONNECTION is poisoned: every later statement
    raises until somebody rolls back — across cursors, because a transaction
    belongs to the connection. Emulating this is the only way to prove the
    cascade is fixed rather than merely absent from one happy path.
    """

    class Error(Exception):
        pass

    def __init__(self, fail_substrings=(), rows=None, poison=True):
        self.fail_substrings = tuple(fail_substrings)
        self.rows = rows or {}
        self.poison = poison
        self.executed = []
        self.rollbacks = 0
        self._result = []
        self.description = None
        self.connection = FakeConn(self)

    def execute(self, sql, params=None):
        flat = " ".join(str(sql).split())
        self.executed.append(flat)
        if self.connection.aborted:
            raise self.Error("current transaction is aborted, commands "
                             "ignored until end of transaction block")
        if any(s in flat for s in self.fail_substrings):
            if self.poison:
                self.connection.aborted = True
            raise self.Error('column "stress_index" does not exist')
        for key, val in self.rows.items():
            if key in flat:
                self._result = val
                return
        self._result = []

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._result[0] if self._result else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _src(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _sql_literals(rel):
    """Every SQL string in a module, EXCLUDING docstrings and prose.

    AST, not a text scan: these modules explain the dead column in prose, and
    a text scan cannot tell a warning about a trap from the trap itself. That
    mistake red-ran the #2071 fence three times.
    """
    tree = ast.parse(_src(rel))
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            docstrings.add(id(body[0].value))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docstrings:
            flat = " ".join(node.value.split())
            if "SELECT " in flat and " FROM " in flat:
                out.append(flat)
    assert out, "no SQL literals found in %s — this fence is now vacuous" % rel
    return out


# ────────────────────────────────────────────── surface 1: dcpi v2 endpoint

_V2_READS = (
    ("FROM water_risk", "water_stress_index", "water_stress"),
    ("FROM eia_retail_rates", "ppa_rate_cents_kwh", "retail_rate"),
)


def _v2_rows():
    """The real shapes, as read off prod Neon on 2026-09-21 for VA."""
    return {
        "FROM market_power_scores": [{
            "market_slug": "northern-virginia", "market_name": "Northern Virginia",
            "state": "VA", "iso": "PJM", "constraint_score": 41.0,
            "excess_power_score": 47.2, "time_to_power_months": 19.0,
            "verdict": "AVOID", "curtailment_pct": 3.1, "computed_at": None,
            "signal_tier": "measured"}],
        "FROM water_risk": [{"water_stress_score": 71.8}],
        "FROM eia_retail_rates": [{"rate_cents_kwh": 10.09}],
    }


@pytest.fixture
def v2(monkeypatch):
    """api_score_market_v2('northern-virginia', _paid=True) against a
    FakeCursor, inside a bare Flask app context — never main.py."""
    from flask import Flask
    app = Flask(__name__)
    monkeypatch.setattr(dcpi, "_ensure_tables", lambda *a, **k: None)

    def run(fail=(), poison=True, rows=None):
        cur = FakeCursor(fail_substrings=fail, poison=poison,
                         rows=_v2_rows() if rows is None else rows)
        monkeypatch.setattr(dcpi, "open_conn", lambda *a, **k: cur.connection)
        with app.app_context():
            resp = dcpi.api_score_market_v2("northern-virginia", _paid=True)
        body = resp[0].get_json() if isinstance(resp, tuple) else resp.get_json()
        return body, cur

    return run


def test_v2_happy_path_returns_every_signal(v2):
    body, _cur = v2()
    v2blk = body["v2"]
    assert v2blk["inputs"]["water_stress_index"] == 4      # WRI High, banded
    assert v2blk["inputs"]["ppa_rate_cents_kwh"] == 10.09
    assert v2blk["read_errors"] == {}


@pytest.mark.parametrize("frag,field,errkey", _V2_READS)
def test_v2_one_dead_poisoning_read_does_not_blank_the_other(v2, frag, field, errkey):
    """THE regression fence for /v2. Each read in turn is the dead one, and it
    poisons the transaction exactly as Postgres does.

    Put the reads back inside a single `with _conn() as c` — or delete the
    rollback in util.db_honesty.try_fetchall — and this goes red.
    """
    body, cur = v2(fail=(frag,), poison=True)
    v2blk = body["v2"]

    assert v2blk["inputs"][field] is None, f"{field} must be null when its read failed"
    assert errkey in v2blk["read_errors"], (
        f"a failed read must name itself; read_errors={v2blk['read_errors']}")
    assert cur.rollbacks > 0, (
        "the failed read left the transaction aborted — nothing rolled it back")

    for other_frag, other_field, other_err in _V2_READS:
        if other_frag == frag:
            continue
        assert v2blk["inputs"][other_field] is not None, (
            f"the dead {frag!r} read cascaded into {other_field} — that is the "
            f"bug, served as HTTP 200")
        assert other_err not in v2blk["read_errors"]


def test_v2_a_failed_water_read_is_not_published_as_the_neutral_fifty(v2):
    """compute_water_risk_score returns 50.0 when it has no signal. That is a
    reasonable default and a terrible fact: a consumer cannot tell it from a
    measured 50 unless the response says the read failed."""
    body, _cur = v2(fail=("FROM water_risk",), poison=True)
    assert body["v2"]["water_risk_score"] == 50.0
    assert body["v2"]["read_errors"].get("water_stress"), (
        "the neutral 50 must be accompanied by the named failure that caused it")


def test_v2_a_dead_connection_is_a_503_not_a_confident_envelope(v2, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("could not connect to server")
    monkeypatch.setattr(dcpi, "open_conn", boom)
    monkeypatch.setattr(dcpi, "_ensure_tables", lambda *a, **k: None)
    from flask import Flask
    with Flask(__name__).app_context():
        resp = dcpi.api_score_market_v2("northern-virginia", _paid=True)
    body, status = resp[0].get_json(), resp[1]
    assert status == 503 and body["read_errors"]["connection"]


# ──────────────────────────────────────── surface 2: dcpi recommend endpoint

def _rec_rows():
    return {
        "FROM market_power_scores": [{
            "market_slug": "northern-virginia", "market_name": "Northern Virginia",
            "state": "VA", "iso": "PJM", "latitude": 38.9, "longitude": -77.4,
            "constraint_score": 41.0, "excess_power_score": 47.2,
            "time_to_power_months": 19.0, "queue_capacity_mw": 900.0,
            "queue_wait_months": 20.0, "reserve_margin_pct": 18.0,
            "stranded_capacity_mw": 120.0, "curtailment_pct": 3.1,
            "verdict": "CAUTION", "top_risks_json": None,
            "top_opportunities_json": None, "signal_tier": "measured",
            "computed_at": None}],
        # ★ FULL name, exactly as the column stores it.
        "FROM eia_retail_rates": [
            {"state_code": "VIRGINIA", "rate_cents_kwh": 10.09, "period": "2026"},
            {"state_code": "EAST NORTH CENTRAL", "rate_cents_kwh": 8.4, "period": "2026"},
        ],
        "FROM water_risk": [{"state_code": "VA", "water_stress_score": 71.8}],
    }


@pytest.fixture
def recommend(monkeypatch):
    from flask import Flask
    app = Flask(__name__)
    monkeypatch.setattr(dcpi, "_ensure_tables", lambda *a, **k: None)

    def run(fail=(), poison=True, rows=None, query=""):
        cur = FakeCursor(fail_substrings=fail, poison=poison,
                         rows=_rec_rows() if rows is None else rows)
        monkeypatch.setattr(dcpi, "open_conn", lambda *a, **k: cur.connection)
        with app.test_request_context("/api/v1/dcpi/recommend?" + query):
            resp = dcpi.api_dcpi_recommend()
        body = resp[0].get_json() if isinstance(resp, tuple) else resp.get_json()
        return body, cur

    return run


def test_recommend_enriches_the_state_it_could_not_reach_before(recommend):
    """Both enrichments were dead: water by UndefinedColumn, retail by a dict
    keyed "VIRGINIA" and looked up by "VA"."""
    body, _cur = recommend()
    assert body["enrichment_coverage"] == {
        "states_with_retail_rate": 1, "states_with_water_stress": 1}
    assert body["read_errors"] == {}
    m = body["ranked_markets"][0]
    assert m["retail_rate_cents_kwh"] == 10.09, (
        "the retail dict must be keyed by USPS code — keying it by the stored "
        "full name is why every lookup missed")
    assert m["water_stress_state"] == 4        # WRI High, banded from 71.8


def test_recommend_census_region_rows_never_masquerade_as_a_state(recommend):
    """eia_retail_rates carries "East North Central". It has no USPS code and
    must not land in a dict that is looked up by one."""
    body, _cur = recommend()
    assert body["enrichment_coverage"]["states_with_retail_rate"] == 1


@pytest.mark.parametrize("frag,errkey,coverage_key", (
    ("FROM eia_retail_rates", "retail_rates", "states_with_retail_rate"),
    ("FROM water_risk", "water_stress", "states_with_water_stress"),
))
def test_recommend_a_dead_enrichment_names_itself_and_spares_the_other(
        recommend, frag, errkey, coverage_key):
    """A failed enrichment silently WIDENS the result set — both filters skip
    a market whose signal is None. The caller has to be able to tell that
    apart from a market that genuinely has no reading."""
    body, cur = recommend(fail=(frag,), poison=True)
    assert errkey in body["read_errors"]
    assert body["enrichment_coverage"][coverage_key] == 0
    assert cur.rollbacks > 0
    other = ("states_with_water_stress" if coverage_key == "states_with_retail_rate"
             else "states_with_retail_rate")
    assert body["enrichment_coverage"][other] == 1, (
        f"the dead {frag!r} read cascaded into the other enrichment")


def test_recommend_filters_actually_bite_now(recommend):
    """The point of fixing the reads. water_stress_max=3 must now exclude a
    band-4 state; while the read was dead it excluded nothing."""
    body, _cur = recommend(query="water_stress_max=3")
    assert body["passed_filters"] == 0, (
        "a band-4 state survived water_stress_max=3 — the filter is still "
        "vacuous")
    loose, _ = recommend(query="water_stress_max=5")
    assert loose["passed_filters"] == 1


def test_recommend_retail_filter_bites_now(recommend):
    body, _cur = recommend(query="max_retail_rate_cents=9")
    assert body["passed_filters"] == 0, (
        "a 10.09 c/kWh state survived max_retail_rate_cents=9 — the retail "
        "lookup is still missing")


# ─────────────────────────────────────── surface 3: land_power _build_analysis

_LPM_READS = (
    ("FROM eia_retail_rates", "power", "industrial_rate_cents_kwh", "_rate_error"),
    ("FROM water_risk", "water", "stress_index", "_error"),
)


def _lpm_rows():
    return {
        "FROM substations": [],
        "FROM market_power_scores": [{
            "verdict": "CAUTION", "excess_power_score": 47.2,
            "constraint_score": 41.0, "time_to_power_months": 19.0,
            "queue_capacity_mw": 900.0, "market_name": "Northern Virginia"}],
        "FROM eia_retail_rates": [{"rate_cents_kwh": 10.09, "period": "2026"}],
        "FROM water_risk": [{"water_stress_score": 71.8}],
        "FROM discovered_facilities": [{"n": 3, "total_mw": 120.0, "max_mw": 60.0}],
    }


@pytest.fixture
def analysis(monkeypatch):
    def run(fail=(), poison=True, rows=None, state="VA"):
        cur = FakeCursor(fail_substrings=fail, poison=poison,
                         rows=_lpm_rows() if rows is None else rows)
        monkeypatch.setattr(lpm, "_conn", lambda *a, **k: cur.connection)
        return lpm._build_analysis(38.9, -77.4, state, 50.0, 40.0), cur

    return run


def test_lpm_happy_path_reads_both_signals(analysis):
    result, _cur = analysis()
    assert result["power"]["industrial_rate_cents_kwh"] == 10.09, (
        "UPPER(state)='VA' matched nothing against a column storing "
        "'Virginia' — both spellings must be matched")
    assert result["water"]["stress_index"] == 4


@pytest.mark.parametrize("frag,block,field,errkey", _LPM_READS)
def test_lpm_one_dead_read_does_not_blank_the_other(
        analysis, frag, block, field, errkey):
    result, cur = analysis(fail=(frag,), poison=True)
    assert result[block].get(field) is None
    assert result[block].get(errkey), (
        f"a failed read must name itself in {block}; got {result[block]!r}")
    assert cur.rollbacks > 0

    for other_frag, other_block, other_field, _e in _LPM_READS:
        if other_frag == frag:
            continue
        assert result[other_block].get(other_field) is not None, (
            f"the dead {frag!r} read cascaded into {other_block}.{other_field}")


def test_lpm_tax_read_survives_a_dead_water_read(analysis):
    """Ordering fence. The water read runs BEFORE the tax read, so a water
    failure that poisons without rolling back takes the tax block with it."""
    result, _cur = analysis(fail=("FROM water_risk",), poison=True)
    assert "current transaction is aborted" not in str(result.get("tax", ""))
    assert result["dcpi"].get("verdict") == "CAUTION"


# ------------------------------------------------------- structural fences

#: The functions this change owns. routes/dcpi.py carries ~32 other
#: `with _conn()` blocks that predate it; converting the whole module is a
#: separate change, and a fence that demanded it would just be turned off.
_OWNED = {
    DCPI: ("api_score_market_v2", "api_dcpi_recommend"),
    LPM: ("_build_analysis",),
    SIM: ("_pull_signals",),
}


def _owned_spans(rel):
    """(name, lineno, end_lineno) for each function this change owns."""
    tree = ast.parse(_src(rel))
    want = _OWNED[rel]
    found = {n.name: (n.lineno, n.end_lineno) for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n.name in want}
    missing = set(want) - set(found)
    assert not missing, (
        f"{rel}: {sorted(missing)} no longer exist — this fence's anchors are "
        f"stale and its structural tests are now vacuous")
    return [(name, *found[name]) for name in want]


def test_the_with_conn_transaction_trap_does_not_come_back():
    """`with <connection>` is what turns one dead read into several null
    signals. It is a TRANSACTION manager, not a closer, and autocommit does
    not save it.

    Scoped to the functions this change owns — see _OWNED.
    """
    checked = 0
    for rel in (DCPI, SIM, LPM):
        tree = ast.parse(_src(rel))
        for name, start, end in _owned_spans(rel):
            for node in ast.walk(tree):
                if not isinstance(node, ast.With) or not (start <= node.lineno <= end):
                    continue
                for item in node.items:
                    call = item.context_expr
                    if isinstance(call, ast.Call):
                        fn = ast.unparse(call.func)
                        assert fn not in ("_conn", "open_conn", "psycopg2.connect"), (
                            f"{rel}:{node.lineno} ({name}) reopened `with {fn}()` "
                            f"— use open_conn() + try/finally + close_quietly()")
            checked += 1
    assert checked == 4, "expected to inspect 4 owned functions, saw %d" % checked


def test_the_dead_water_column_stays_gone_on_every_surface():
    """usgs_water_stress has no stress column at all, and its water_level_ft
    proxy was withdrawn 2026-07-07 for reading INVERTED."""
    for rel in ALL_ROUTES:
        for sql in _sql_literals(rel):
            assert "usgs_water_stress" not in sql, (
                f"{rel}: the water read is back on usgs_water_stress, which "
                f"carries no stress column and whose groundwater proxy is "
                f"direction-inverted: {sql!r}")
            assert not re.search(r"\bstress_index\b", sql), (
                f"{rel}: stress_index has never existed on any table here: {sql!r}")
            assert not re.search(r"\bwater_level_ft\b", sql), (
                f"{rel}: water_level_ft is the INVERTED proxy withdrawn "
                f"2026-07-07, not a substitute for a stress score: {sql!r}")


def test_no_surface_falls_back_to_the_all_null_baseline_column():
    """water_risk.baseline_water_stress is NULL on all 51 rows. A COALESCE
    onto it buys nothing and hides that the real column went unread."""
    for rel in ALL_ROUTES + ("util/water_stress.py",):
        for sql in _sql_literals(rel) if rel != "util/water_stress.py" else \
                [s for s in _sql_literals(rel)]:
            assert "baseline_water_stress" not in sql, (
                f"{rel} reads baseline_water_stress, which is NULL on every row")


def test_the_water_reads_target_the_store_that_has_the_signal():
    for rel in ALL_ROUTES:
        sqls = " || ".join(_sql_literals(rel))
        src = _src(rel)
        # dcpi's bulk read uses the shared STATE_WATER_STRESS_SQL constant, so
        # accept either the literal or the import of the one that carries it.
        has_literal = "water_stress_score" in sqls and "FROM water_risk" in sqls
        has_shared = "STATE_WATER_STRESS_SQL" in src
        assert has_literal or has_shared, (
            f"{rel}: water stress must come from water_risk.water_stress_score "
            f"— the verified WRI Aqueduct roll-up")


def test_both_retail_reads_match_the_full_state_name():
    """eia_retail_rates.state holds "Virginia", not "VA"."""
    from util.us_states import state_match_pair
    assert state_match_pair("VA") == ("VA", "VIRGINIA")

    checked = 0
    for rel in ALL_ROUTES:
        for sql in _sql_literals(rel):
            if "FROM eia_retail_rates" not in sql:
                continue
            flat = " ".join(sql.split())
            if "WHERE LOWER(sector) = 'industrial' ORDER BY" in flat \
                    or "UPPER(state) = %s" not in flat and "IN (%s, %s)" not in flat:
                # The bulk read has no per-state WHERE; it is fenced by
                # test_recommend_* instead.
                continue
            assert "UPPER(state) IN (%s, %s)" in flat, (
                f"{rel}: the retail read must match BOTH the USPS code and "
                f"the full state name; {sql!r} matches only one spelling")
            checked += 1
    assert checked >= 2, (
        "fewer than two per-state retail reads found — this fence's anchors "
        "are stale and it is now vacuous")


def test_reads_go_through_the_importable_honesty_helper():
    for rel, minimum in ((DCPI, 3), (LPM, 2), (SIM, 3)):
        src = _src(rel)
        assert "from util.db_honesty import" in src, (
            f"{rel}: the reads must use util.db_honesty, not a function-local "
            f"copy — a fence can assert an import, it cannot inspect a "
            f"private helper")
        n = src.count("try_fetchone(cur,") + src.count("try_fetchall(cur,")
        assert n >= minimum, (
            f"{rel}: only {n} reads go through try_fetch*; a bare cur.execute "
            f"in a try/except swallows the error again")


def test_the_band_has_exactly_one_implementation():
    """Three surfaces need the 0-100 -> 1-5 band. util/us_states.py records
    where hand-copies end: seven copies of one predicate, and a census that
    could not check any of them."""
    for rel in ALL_ROUTES:
        src = _src(rel)
        assert "from util.water_stress import" in src, (
            f"{rel} must import the shared band, not re-derive it")
        assert "12.5" not in src or "util.water_stress" in src, (
            f"{rel} appears to re-implement the band edges inline")


# ------------------------------------------------------------- band fences

def test_water_band_is_direction_correct():
    """The 2026-07-07 pause was caused by an INVERTED proxy — arid states read
    LESS stressed than wet ones. Assert the opposite, as the ingest does."""
    assert water_band(71.8) > water_band(12.5), "the 1-5 band is inverted"
    assert water_band(0.0) == 1 and water_band(100.0) == 5


def test_water_band_covers_the_five_wri_categories():
    """WRI publishes bws_cat -1..4, which the ingest normalises to
    0/25/50/75/100. Each must land on its own band."""
    assert [water_band(v) for v in (0.0, 25.0, 50.0, 75.0, 100.0)] == [1, 2, 3, 4, 5]


def test_an_unread_water_score_never_becomes_a_number():
    assert water_band(None) is None


def test_the_shared_band_matches_the_one_site_simulator_shipped():
    """#5259 pinned these exact values. Centralising the implementation must
    not have moved any state a band."""
    import routes.site_simulator as ss
    assert [ss._water_band(v) for v in (0.0, 25.0, 50.0, 75.0, 100.0)] == [1, 2, 3, 4, 5]
    assert ss._water_band(71.8) == 4 and ss._water_band(12.5) == 2
