"""Read-honesty fence for GET /api/v1/site/simulate-buildout — 2026-09-21.

THE BUG THIS EXISTS TO PREVENT, measured live against prod Neon.
routes/site_simulator.py gathered four signals inside `with _conn() as c` —
psycopg2's connection context manager, which is a TRANSACTION manager and not
a closer, so `autocommit` did not apply. The water read named
`usgs_water_stress.stress_index`:

    SELECT AVG(stress_index) AS s FROM usgs_water_stress WHERE UPPER(state)=%s
    -> UndefinedColumn: column "stress_index" does not exist

That table has never had a stress column of any kind. Its real columns are
site_id / site_name / latitude / longitude / state / county / aquifer_name /
well_depth_ft / water_level_ft / water_level_date / site_type, it covers 16
states, and `water_level_ft` is the groundwater proxy WITHDRAWN on 2026-07-07
for reading INVERTED (routes/interconnection_queues.py still refuses to score
off it). Each read sat in a bare `except Exception: pass` with no rollback, so
the failure aborted the transaction and every LATER read died of
InFailedSqlTransaction. VA / TX / OH were all served:

    retail_rate_cents_kwh: null   water_stress_index: null
    dcpi_verdict:          null   tax_pct_offset:     0.0

behind HTTP 200, under a methodology string asserting "DCPI verdict +
water_stress + retail rate pulled live".

A SECOND, INDEPENDENT LIE sat in the retail read. `eia_retail_rates.state`
holds FULL names, so `UPPER(state) = 'VA'` matched nothing while 'VIRGINIA'
returned 10 rows (industrial 10.09 c/kWh, period 2026). That query never
failed; it just quietly answered "no data".

WHAT IS PINNED HERE
  * One failing read cannot blank the reads after it — asserted with real
    psycopg2 abort semantics (poison=True), for EVERY read in turn, so
    removing a rollback turns these red rather than passing on a happy path.
  * A read that failed publishes null and names itself. tax_pct_offset in
    particular must be null, never the 0.0 that production served.
  * The dead table/column stay gone and the water read points at the store
    that actually carries the signal.
  * The retail read matches the full state name.
  * The 1-5 band is direction-correct: arid out-ranks wet.
  * The methodology cannot claim a read it did not make.
  * Anti-vacuous floors, per the #2062 lesson: a fence that goes green because
    the thing it inspects became empty is worse than no fence.

House rules: no DB, never import main, nothing at module scope.
Run:  python3 -m pytest tests/test_site_simulator_read_honesty.py -v
"""
from __future__ import annotations

import ast
import os
import re

import pytest

import routes.site_simulator as ss

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROUTE = "routes/site_simulator.py"

# Every read _pull_signals makes, keyed by the SQL fragment that identifies it,
# and the signal + read_errors key it owns.
# `owner` says who writes the SQL: "route" literals live in this module,
# while the tax read is issued by util.tax_incentives through our cursor.
READS = (
    ("FROM eia_retail_rates",    "retail_rate_cents_kwh", "retail_rate",  "route"),
    ("FROM water_risk",          "water_stress_score",    "water_stress", "route"),
    ("FROM market_power_scores", "dcpi_verdict",          "dcpi",         "route"),
    ("FROM tax_incentives_neon", "tax_pct_offset",        "tax",          "accessor"),
)


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


def _live_rows():
    """The real shapes, as read off prod Neon on 2026-09-21 for VA."""
    return {
        "FROM eia_retail_rates": [{"rate_cents_kwh": 10.09}],
        "FROM water_risk": [{"water_stress_score": 71.8}],
        "FROM market_power_scores": [{
            "verdict": "AVOID", "excess_power_score": 47.2,
            "constraint_score": 41.0, "time_to_power_months": 19.0}],
        "FROM tax_incentives_neon": [{
            "state_abbr": "VA", "state_name": "Virginia",
            "sales_tax_exempt": True, "property_tax_abatement": True,
            "energy_incentive": False, "data_center_specific": True,
            "incentive_details": "VA sales & use tax exemption",
            "qualifying_investment": None, "qualifying_jobs": None,
            "duration_years": None, "max_benefit": None,
            "source_url": None, "last_updated": "2026-03-17"}],
    }


@pytest.fixture
def signals(monkeypatch):
    """_pull_signals('VA') against a FakeCursor, with the real registry
    defaults in play so the tax path exercises util.tax_incentives."""
    import tax_incentives_routes
    monkeypatch.setattr(tax_incentives_routes, "_SERVED", [None])

    def run(fail=(), poison=True, rows=None, state="VA"):
        cur = FakeCursor(fail_substrings=fail, poison=poison,
                         rows=_live_rows() if rows is None else rows)
        monkeypatch.setattr(ss, "open_conn", lambda *a, **k: cur.connection)
        return ss._pull_signals(state), cur

    return run


def _src():
    with open(os.path.join(ROOT, ROUTE), encoding="utf-8") as fh:
        return fh.read()


def _sql_literals():
    """Every SQL string in the module, EXCLUDING docstrings and prose.

    AST, not a text scan: the module explains the dead column in prose, and a
    text scan cannot tell a warning about a trap from the trap itself. That
    mistake red-ran the #2071 fence three times.
    """
    tree = ast.parse(_src())
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
            # ★ SQL only. The methodology string is a non-docstring literal
            # that legitimately says "water_stress_index" — which CONTAINS
            # "stress_index". Scanning prose for a dead column name is how a
            # fence ends up reading its own warning as the trap.
            if "SELECT " in flat and " FROM " in flat:
                out.append(flat)
    assert out, "no SQL literals found in %s — this fence is now vacuous" % ROUTE
    return out


# ------------------------------------------------------- the cascade fences

def test_every_read_is_exercised_by_the_happy_path():
    """Anti-vacuous floor. Every fence below drives _pull_signals through a
    FakeCursor; if a read is renamed or dropped, the fixtures stop matching and
    the cascade tests would pass while proving nothing."""
    sqls = " || ".join(_sql_literals())
    src = _src()
    for frag, _signal, _errkey, owner in READS:
        found = frag in sqls if owner == "route" else "state_incentive(cur," in src
        assert found, (
            f"{ROUTE} no longer issues a read matching {frag!r} — this fence's "
            f"fixtures are stale and its cascade tests are now vacuous")


def test_happy_path_returns_every_signal(signals):
    sig, _cur = signals()
    assert sig["retail_rate_cents_kwh"] == 10.09
    assert sig["water_stress_score"] == 71.8
    assert sig["water_stress_index"] == 4      # WRI High
    assert sig["dcpi_verdict"] == "AVOID"
    assert sig["tax_pct_offset"] == pytest.approx(0.16)   # 5% + 8% + 3%
    assert sig["read_errors"] == {}


@pytest.mark.parametrize("frag,signal,errkey,owner", READS)
def test_one_dead_poisoning_read_does_not_blank_the_others(
        signals, frag, signal, errkey, owner):
    """THE regression fence. Each read in turn is the dead one, and it poisons
    the transaction exactly as Postgres does. Every OTHER signal must survive.

    Delete the unpoison/rollback in util.db_honesty.try_fetchall — or put the
    reads back inside a single `with <conn>` — and this goes red.
    """
    sig, cur = signals(fail=(frag,), poison=True)

    assert sig[signal] is None, f"{signal} must be null when its read failed"
    assert errkey in sig["read_errors"], (
        f"a failed read must name itself; read_errors={sig['read_errors']}")
    assert cur.rollbacks > 0, (
        "the failed read left the transaction aborted — nothing rolled it back")

    for other_frag, other_signal, other_err, _owner in READS:
        if other_frag == frag:
            continue
        assert sig[other_signal] is not None, (
            f"the dead {frag!r} read cascaded into {other_signal} — that is "
            f"the #2071 bug, served as HTTP 200")
        assert other_err not in sig["read_errors"]


def test_a_failed_tax_read_publishes_null_not_the_zero_production_served(signals):
    """Production served tax_pct_offset: 0.0 for every state whose record
    needed a DB read. A consumer can branch on null; it cannot detect a
    failure told as 0.0, and here 0.0 is an affirmative claim that the state
    offers no incentive."""
    sig, _cur = signals(fail=("FROM tax_incentives_neon",), poison=True)
    assert sig["tax_pct_offset"] is None, (
        "a tax read that FAILED must publish null — 0.0 is reserved for a "
        "state we read and that genuinely offers no offset")
    assert "tax" in sig["read_errors"]


def test_a_state_no_store_knows_is_a_measured_zero_not_a_failure(signals):
    """The other direction: a genuine absence must stay 0.0, or this fence
    would have traded one lie for another."""
    # "ZZ" is in neither the registry nor the snapshot, so the accessor
    # returns None having read successfully.
    sig, _cur = signals(state="ZZ")
    assert sig["tax_pct_offset"] == 0.0
    assert "tax" not in sig["read_errors"]


def test_a_dead_connection_fails_every_signal_closed(signals, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("could not connect to server")
    monkeypatch.setattr(ss, "open_conn", boom)
    sig = ss._pull_signals("VA")
    assert sig["read_errors"].get("connection")
    assert sig["tax_pct_offset"] is None and sig["dcpi_verdict"] is None


# ------------------------------------------------------- structural fences

def test_the_with_conn_transaction_trap_does_not_come_back():
    """`with <connection>` is what turns one dead read into four null signals.
    It is a TRANSACTION manager, not a closer, and autocommit does not save it.
    """
    tree = ast.parse(_src())
    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        for item in node.items:
            call = item.context_expr
            if isinstance(call, ast.Call):
                name = ast.unparse(call.func)
                assert name not in ("_conn", "open_conn", "psycopg2.connect"), (
                    f"{ROUTE}:{node.lineno} reopened `with {name}()` — use "
                    f"open_conn() + try/finally + close_quietly()")


def test_reads_go_through_the_importable_honesty_helper():
    src = _src()
    assert "from util.db_honesty import" in src, (
        "the reads must use util.db_honesty, not a function-local copy — a "
        "fence can assert an import, it cannot inspect a private helper")
    assert src.count("try_fetchone(cur,") >= 3, (
        "fewer reads go through try_fetchone than this route makes; a bare "
        "cur.execute in a try/except swallows the error again")
    assert "except Exception:\n                pass" not in src, (
        "a bare `except Exception: pass` around a read is exactly the swallow "
        "that made the live bug invisible")


def test_the_dead_water_column_stays_gone():
    """usgs_water_stress has no stress column at all, and its water_level_ft
    proxy was withdrawn on 2026-07-07 for reading INVERTED."""
    for sql in _sql_literals():
        assert "usgs_water_stress" not in sql, (
            "the water read is back on usgs_water_stress, which carries no "
            "stress column and whose groundwater proxy is direction-inverted")
        assert not re.search(r"\bstress_index\b", sql), (
            "stress_index has never existed on any table here")


def test_the_water_read_targets_the_store_that_has_the_signal():
    sqls = " || ".join(_sql_literals())
    assert "water_stress_score" in sqls and "FROM water_risk" in sqls, (
        "water stress must come from water_risk.water_stress_score — the "
        "verified WRI Aqueduct roll-up")


def test_the_retail_read_matches_the_full_state_name():
    """eia_retail_rates.state holds "Virginia", not "VA". Matching only the
    2-letter code is a clean query that answers "no data" forever."""
    from util.us_states import state_match_pair
    assert state_match_pair("VA") == ("VA", "VIRGINIA")

    retail = [s for s in _sql_literals() if "FROM eia_retail_rates" in s]
    assert retail, "no eia_retail_rates read found in the route"
    for sql in retail:
        assert "UPPER(state) IN (%s, %s)" in " ".join(sql.split()), (
            "the retail read must match BOTH the USPS code and the full "
            "state name; %r matches only one spelling" % sql)


def test_the_route_reads_tax_incentives_through_the_one_accessor():
    """#5146: util.tax_incentives is the only read path. A direct
    tax_incentives_neon query republishes programs paused or repealed since
    the snapshot froze on 2026-03-17."""
    src = _src()
    assert "from util.tax_incentives import state_incentive" in src
    for sql in _sql_literals():
        assert "tax_incentives_neon" not in sql, (
            f"{ROUTE} queries the frozen snapshot directly: {sql!r}")


# ------------------------------------------------------------- band fences

def test_water_band_is_direction_correct():
    """The 2026-07-07 pause was caused by an INVERTED proxy — arid states read
    LESS stressed than wet ones. Assert the opposite, as the ingest does."""
    arid = ss._water_band(71.8)      # AZ, live 2026-09-21
    wet = ss._water_band(12.5)       # AK, live 2026-09-21
    assert arid > wet, "the 1-5 band is inverted"
    assert ss._water_band(0.0) == 1 and ss._water_band(100.0) == 5


def test_water_band_covers_the_five_wri_categories():
    """WRI publishes bws_cat -1..4, which the ingest normalises to
    0/25/50/75/100. Each must land on its own band."""
    assert [ss._water_band(v) for v in (0.0, 25.0, 50.0, 75.0, 100.0)] == \
        [1, 2, 3, 4, 5]


def test_an_unread_water_score_never_becomes_a_number():
    assert ss._water_band(None) is None


def test_the_high_water_stress_flag_fires_on_the_wri_high_band():
    """_risk_flags keys on `water_stress_index >= 4`, so the banding and the
    flag have to agree about what "high" means."""
    assert "high_water_stress" in ss._risk_flags(
        {"water_stress_index": ss._water_band(71.8)}, 50.0)
    assert "high_water_stress" not in ss._risk_flags(
        {"water_stress_index": ss._water_band(12.5)}, 50.0)


# ------------------------------------------------------- methodology fence

def test_methodology_cannot_claim_a_read_it_did_not_make(signals):
    """The old string asserted "DCPI verdict + water_stress + retail rate
    pulled live" on every response, including the ones where all three were
    null. A methodology its own response can falsify is not a methodology."""
    sig, _cur = signals(fail=("FROM water_risk",), poison=True)
    text = ss._methodology(sig, sig["retail_rate_cents_kwh"])
    assert "water_stress" in text
    assert "read_errors" in text or "Failed reads" in text, (
        "the methodology must point at the failure, not assert the read")

    clean, _cur2 = signals()
    clean_text = ss._methodology(clean, clean["retail_rate_cents_kwh"])
    assert "Failed reads" not in clean_text


def test_methodology_admits_the_retail_fallback(signals):
    """7.5 c/kWh is a national average substituted for a missing reading. The
    response must say so rather than let the number pass as measured."""
    rows = _live_rows()
    rows["FROM eia_retail_rates"] = []
    sig, _cur = signals(rows=rows)
    assert sig["retail_rate_cents_kwh"] is None
    text = ss._methodology(sig, sig["retail_rate_cents_kwh"])
    assert "7.5" in text and "fallback" in text.lower()
