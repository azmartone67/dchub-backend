"""The EIA-860M retirement refresh, executed against a real Postgres (2026-09-12).

tests/test_eia_retirements_refresh.py pins the gate, the fetch guards and the
record shape with no database. Only a database shows the refresh as ONE
transaction over rows that already exist:

  A  re-filed with a new date and no coordinates -> date and source_month move,
     coordinates kept, ingested_at restamped
  B  a future retirement the new filing no longer lists -> deleted: the phantom
     headroom an upsert-only refresh would keep serving under the new vintage
  C  planned, with no plant id -> deleted: no refresh could ever update it
  D  status 'retired' -> untouched: the prune is scoped to planned rows
  E  re-filed with a month that does not parse -> left exactly as it was
  F  new in the filing -> inserted
  G  new, no coordinates, county in another case -> the substations median

and then routes/served_table_freshness.measure — the code the dead-man board
runs — reading that same table frozen before the refresh and fresh after it.
A refused refresh, and one that fails after its upsert and prune have already
executed, must both leave the table exactly as it was.

Nothing is copied: the DDL, the statements and the orchestrator are the shipped
module's own; only the EIA fetch is replaced. Set EIA_RETIREMENTS_SQL_DSN to run
it. CI passes the db-parity service DSN and then asserts this file did not skip.
"""
import datetime as dt
import os
import sys

import pytest

psycopg2 = pytest.importorskip("psycopg2")

DSN = os.environ.get("EIA_RETIREMENTS_SQL_DSN", "").strip()
pytestmark = pytest.mark.skipif(
    not DSN, reason="EIA_RETIREMENTS_SQL_DSN not set — no Postgres to run against")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import eia_retirements as er  # noqa: E402
from routes import served_table_freshness as stf  # noqa: E402

UTC = dt.timezone.utc
PLANNED = er.STATUS
COLS = ("eia_plant_id, generator_id, plant_name, state, county, lat, lng, "
        "capacity_mw, fuel_category, prime_mover, ba_code, retirement_date, "
        "status, source_month, ingested_at")


@pytest.fixture
def cur():
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    c = conn.cursor()
    c.execute("DROP TABLE IF EXISTS generator_retirements")
    c.execute("DROP TABLE IF EXISTS substations")
    c.execute("CREATE TABLE substations (state TEXT, county TEXT, "
              "lat DOUBLE PRECISION, lng DOUBLE PRECISION)")
    c.execute("INSERT INTO substations VALUES ('TX','Harris',29.0,-95.0), "
              "('TX','Harris',30.0,-96.0), ('TX','Harris',31.0,-97.0)")
    c.execute(er._DDL)
    yield c
    c.close()
    conn.close()


def _row(plant, gen, retires, *, status=PLANNED, period="2026-04", age_days=63.0,
         lat=30.5, lng=-95.5, cap=100.0):
    return (plant, gen, f"Plant {plant}", "TX", "Harris", lat, lng, cap,
            "Conventional Steam Coal", "ST", "ERCO", retires, status, period,
            dt.datetime.now(UTC) - dt.timedelta(days=age_days))


def _seed(cur, *rows):
    for r in rows:
        cur.execute(f"INSERT INTO generator_retirements ({COLS}) "
                    f"VALUES ({', '.join(['%s'] * 15)})", r)


def _dump(cur):
    cur.execute(f"SELECT id, {COLS} FROM generator_retirements ORDER BY id")
    return cur.fetchall()


def _by_key(cur):
    cur.execute("SELECT eia_plant_id, generator_id, status, retirement_date, "
                "capacity_mw, lat, lng, source_month, ingested_at "
                "FROM generator_retirements")
    return {(r[0], r[1], r[2]): r[3:] for r in cur.fetchall()}


def _eia(plant, gen, month, county="Harris", cap="100", lat=None, lng=None):
    x = {"plantid": str(plant), "generatorid": gen,
         "planned-retirement-year-month": month, "plantName": f"Plant {plant}",
         "stateid": "TX", "county": county, "nameplate-capacity-mw": cap,
         "technology": "Conventional Steam Coal", "prime_mover_code": "ST",
         "balancing_authority_code": "ERCO"}
    if lat is not None:
        x.update(latitude=lat, longitude=lng)
    return x


def _wire(monkeypatch, period, rows):
    """Replace ONLY the EIA calls; every connection goes to the test database."""
    fetched, beats = [], []

    def fetch(api_key, p, get_json=None):
        fetched.append(p)
        return rows

    monkeypatch.setattr(er, "_latest_period", lambda key: period)
    monkeypatch.setattr(er, "fetch_planned_retirements", fetch)
    monkeypatch.setattr(er, "_connect", lambda url: psycopg2.connect(DSN))
    return fetched, beats, (lambda feed, **kw: beats.append(dict(kw, feed=feed)))


def _run(beat, force=True):
    return er.run_eia_retirements_ingest("postgres://via-_connect", "key",
                                         force=force, beat=beat)


def _served(cur):
    """The dead-man board's own measurement of this table, read-only."""
    cand = {"table": "generator_retirements", "tier": 1, "why": ["freshness_claimed"],
            "mcp_tools": [], "claim_routes": [], "zero_writer": False}
    cur.execute("BEGIN READ ONLY")
    try:
        return stf.measure(cur, [cand], dt.datetime.now(UTC))[0]
    finally:
        cur.execute("ROLLBACK")


def test_a_refresh_mirrors_the_latest_filing_in_one_commit(cur, monkeypatch):
    _seed(cur,
          _row(100, "1", "2027-01-01", lat=30.0, lng=-90.0, cap=500.0),  # A
          _row(101, "1", "2027-03-01"),                                  # B
          _row(None, "X", "2027-05-01"),                                 # C
          _row(103, "1", "2026-01-01", status="retired"),                # D
          _row(104, "1", "2026-12-01"))                                  # E
    assert _served(cur)["state"] == "frozen"

    fetched, beats, beat = _wire(monkeypatch, "2026-07", [
        _eia(100, "1", "2028-06", cap="480"),                  # A: no coordinates now
        _eia(104, "1", "2026-13"),                             # E: unparseable month
        _eia(105, "2", "2029-01", lat="40.1", lng="-80.2"),    # F
        _eia(106, "1", "2030-02", county="harris"),            # G: no coordinates
    ])
    out = _run(beat)

    assert out["ok"] is True, out
    assert fetched == ["2026-07"]
    assert (out["inserted"], out["updated"], out["pruned"],
            out["median_coord_fallbacks"]) == (2, 1, 2, 1), out
    assert (out["held_before"], out["total_planned"], out["geocoded"]) == (4, 4, 4), out
    assert out["skipped_detail"]["bad_month"] == 1

    rows = _by_key(cur)
    assert set(rows) == {(100, "1", PLANNED), (103, "1", "retired"),
                         (104, "1", PLANNED), (105, "2", PLANNED), (106, "1", PLANNED)}
    now = dt.datetime.now(UTC)
    retires, cap, lat, lng, period, stamped = rows[(100, "1", PLANNED)]
    assert (str(retires), cap, lat, lng, period) == ("2028-06-01", 480.0, 30.0, -90.0, "2026-07")
    assert now - stamped < dt.timedelta(minutes=5)
    retires, _cap, _lat, _lng, period, stamped = rows[(104, "1", PLANNED)]
    assert (str(retires), period) == ("2026-12-01", "2026-04")
    assert now - stamped > dt.timedelta(days=60)
    assert rows[(103, "1", "retired")][4] == "2026-04"
    assert rows[(106, "1", PLANNED)][2:4] == (30.0, -96.0)

    served = _served(cur)
    assert served["state"] == "fresh" and served["age_days"] < 0.1, served
    assert [(b["feed"], b["status"], b["rows"], b["cad"]) for b in beats] == \
        [(er.FEED, "success", 3, er.CADENCE_HOURS)]
    assert "2 new, 1 refreshed, 2 no longer listed" in beats[0]["note"]


def test_a_same_period_refresh_restamps_rows_it_did_not_change(cur, monkeypatch):
    """The one-shot loader's stamp bug: re-confirming every row while inserting
    none left MAX(ingested_at) old, so a healthy table still read frozen."""
    _seed(cur, _row(600, "1", "2028-01-01", period="2026-07",
                    age_days=er.REFRESH_MAX_AGE_DAYS + 40))
    assert _served(cur)["state"] == "frozen"
    fetched, beats, beat = _wire(monkeypatch, "2026-07", [_eia(600, "1", "2028-01")])
    out = _run(beat, force=False)
    assert out["ok"] is True and out["due"] is True and fetched == ["2026-07"], out
    assert (out["inserted"], out["updated"], out["pruned"]) == (0, 1, 0), out
    assert _served(cur)["state"] == "fresh"


def test_the_gate_leaves_a_current_table_alone(cur, monkeypatch):
    _seed(cur, _row(500, "1", "2028-01-01", period="2026-07", age_days=2))
    before = _dump(cur)
    fetched, beats, beat = _wire(monkeypatch, "2026-07", [_eia(500, "1", "2029-01")])
    out = _run(beat, force=False)
    assert out["ok"] is True and out["due"] is False, out
    assert fetched == [] and beats == []
    assert _dump(cur) == before


def test_the_first_refresh_creates_the_table(cur, monkeypatch):
    cur.execute("DROP TABLE generator_retirements")
    _fetched, beats, beat = _wire(monkeypatch, "2026-07",
                                  [_eia(700, "1", "2029-01", lat="35", lng="-85")])
    out = _run(beat, force=False)
    assert out["ok"] is True, out
    assert out["reason"] == "the table holds no planned retirements"
    assert (out["inserted"], out["total_planned"], out["pruned"]) == (1, 1, 0), out


def test_a_filing_under_the_keep_ratio_is_refused_and_writes_nothing(cur, monkeypatch):
    _seed(cur, *[_row(200 + i, "1", "2028-01-01") for i in range(10)])
    before = _dump(cur)
    _fetched, beats, beat = _wire(monkeypatch, "2026-07",
                                  [_eia(200, "1", "2028-02"), _eia(201, "1", "2028-02")])
    out = _run(beat)
    assert out["ok"] is False and "refusing to prune" in out["error"], out
    assert _dump(cur) == before
    assert [(b["feed"], b["status"]) for b in beats] == [(er.FEED, "error")]


def test_a_period_older_than_the_one_held_is_refused(cur, monkeypatch):
    _seed(cur, _row(400, "1", "2028-01-01", period="2026-06"))
    before = _dump(cur)
    _fetched, _beats, beat = _wire(monkeypatch, "2026-05", [_eia(400, "1", "2029-01")])
    out = _run(beat)
    assert out["ok"] is False and "older than" in out["error"], out
    assert _dump(cur) == before


def test_a_failure_after_the_upsert_and_prune_rolls_both_back(cur, monkeypatch):
    """The coordinate fallback runs LAST. With substations gone it fails after
    the upsert has updated 300 and inserted 302 and the prune has deleted 301
    — and none of that may survive."""
    _seed(cur, _row(300, "1", "2028-01-01", lat=None, lng=None), _row(301, "1", "2028-01-01"))
    cur.execute("DROP TABLE substations")
    before = _dump(cur)
    _fetched, beats, beat = _wire(monkeypatch, "2026-07",
                                  [_eia(300, "1", "2029-01"), _eia(302, "1", "2029-01")])
    out = _run(beat)
    assert out["ok"] is False and "substations" in out["error"], out
    assert _dump(cur) == before
    assert [b["status"] for b in beats] == ["error"]
