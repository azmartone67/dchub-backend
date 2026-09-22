"""GET /api/v1/energy/site-analysis scores only what it measured (2026-09-22).

Measured live 2026-09-22 ~08:05Z: Ashburn, VA (lat=39.04&lng=-77.48&radius=25)
came back with every count 0 and was still published as overallScore 30,
"Challenging", keyless and with a free key. One test group per defect:

  R  the radius. Callers send kilometres (25, 50) as well as metres (50000);
     read as metres, radius=25 searched a 50 m box and found nothing. At
     radius=25000 the same point had 148 substations.
  U  a layer that could not be read was scored 0 beside the 30-point base, and a
     failed statement aborted the transaction the next layers ran in.
  G  pipelines are points, and the pipeline distance read only polylines, so
     gasScore was 0 wherever pipelines were found (18 at Ashburn).
  T  transmission read columns production's infrastructure_layers does not
     have, so it was always empty and scored 0.
  P  plants came from discovered_power_plants (overlapping source records, some
     misplaced), cut at 100, so the published count was the cap.
  N  a missing voltage or generation figure was published as 0.

The route runs for real on a Flask app. Only the database connection and the
nearest-line lookup (site_planner.find_nearest_transmission_measured, which
tests/test_transmission_readers_sql.py runs on Postgres) are replaced. The fake
database applies each statement's bounding box and LIMIT and refuses a
statement it does not know, so a radius read in the wrong unit finds nothing,
as production did. The caller is X-Internal-Key, which gets the full answer;
the Pro-only gate on this route is pinned in tests/test_land_power_pro_only.py.
"""
import math
import re
import sys
import types

import pytest
from flask import Flask

import energy_infrastructure_routes as eir

SECRET = "energy-site-analysis-test-internal-key"
SITE = (39.04, -77.48)


def _at(km_north, km_east):
    """A point km_north and km_east of SITE."""
    return (SITE[0] + km_north / 111.32,
            SITE[1] + km_east / (111.32 * math.cos(math.radians(SITE[0]))))


def _sub(name, km_north, km_east, max_volt=230.0):
    # name, city, state, zip, type, status, owner, max_volt, min_volt, lat, lng
    return (name, "Ashburn", "VA", "20147", "SUBSTATION", "IN SERVICE", "Dominion",
            max_volt, 115.0, *_at(km_north, km_east))


SUBSTATIONS = [
    _sub("Ashburn 500kV", 1.2, 0.5, 500.0),   # 1.3 km
    _sub("Beaumeade", -4.0, 6.0),             # 7.2 km
    _sub("Corner", 20.0, 20.0),               # 28.3 km: inside the 25 km box, outside the circle
    _sub("Far", 0.0, 40.0),                   # 40 km
]
# name, operator, pipeline_type, diameter_inches, status, lat, lng
PIPELINES = [("Line 1", "Columbia Gas Trans Co", "Interstate", 30.0, "active", *_at(1.0, 0.0)),
             ("Line 2", "Transcontinental Gas PL", "Interstate", 36.0, "active", *_at(0.0, -9.0))]
# name, utility_name, nameplate_capacity_mw, primary_fuel, lat, lng
PLANTS = [("Loudoun Station", "Example Utility", 285.0, "Natural Gas", *_at(3.0, 2.0)),
          ("Rooftop Array", "Example Solar", None, "Solar", *_at(-6.0, 1.0))]
LINE = {"line_name": "PLEASANT VIEW", "voltage_kv": 230.0,
        "owner": "VIRGINIA ELECTRIC & POWER CO", "status": "IN SERVICE", "volt_class": None,
        "distance_miles": 1.0, "matched_substation": "ASHBURN"}

LAT_LNG = {"substations": (9, 10), "gas_pipelines": (5, 6), "power_plants_eia": (4, 5)}


class _DB:
    def __init__(self):
        self.tables = {"substations": list(SUBSTATIONS), "gas_pipelines": list(PIPELINES),
                       "power_plants_eia": list(PLANTS)}
        self.fail = set()        # tables whose statement raises
        self.statements = []     # the table each statement read, in order
        self.rollbacks = 0
        self.aborted = False     # a statement failed and nothing rolled it back


class _Cur:
    def __init__(self, db):
        self.db, self._rows = db, []

    def execute(self, sql, params=None):
        text = " ".join(sql.split())
        found = re.search(r"\bFROM (\w+)", text)
        table = found.group(1) if found else ""
        self.db.statements.append(table)
        if self.db.aborted:
            raise RuntimeError("InFailedSqlTransaction: current transaction is aborted")
        if table not in self.db.tables:
            self.db.aborted = True
            raise AssertionError(f"the fake database has no answer for: {text[:160]}")
        if table in self.db.fail:
            self.db.aborted = True
            raise RuntimeError(f"UndefinedColumn on {table}")
        la0, la1, lo0, lo1 = (float(p) for p in params[:4])
        limit = re.search(r"LIMIT (\d+|%s)", text).group(1)
        limit = int(params[-1] if limit == "%s" else limit)
        lat_i, lng_i = LAT_LNG[table]
        rows = [r for r in self.db.tables[table]
                if la0 <= r[lat_i] <= la1 and lo0 <= r[lng_i] <= lo1]
        rows.sort(key=lambda r: math.hypot(r[lat_i] - SITE[0], r[lng_i] - SITE[1]))
        self._rows = rows[:limit]

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class _Conn:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return _Cur(self.db)

    def rollback(self):
        self.db.rollbacks += 1
        self.db.aborted = False

    def close(self):
        pass


@pytest.fixture
def db(monkeypatch):
    state = _DB()
    main_stub = sys.modules["main"]
    monkeypatch.setattr(main_stub, "get_pg_connection", lambda: _Conn(state), raising=False)
    monkeypatch.setattr(main_stub, "return_pg_connection", lambda conn: None, raising=False)
    return state


@pytest.fixture
def tx(monkeypatch):
    import site_planner
    holder = types.SimpleNamespace(answer=(dict(LINE), True), calls=[])

    def lookup(lat, lng, max_distance_miles=15):
        holder.calls.append(max_distance_miles)
        return holder.answer
    monkeypatch.setattr(site_planner, "find_nearest_transmission_measured", lookup)
    return holder


@pytest.fixture
def client(db, tx, monkeypatch):
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", SECRET)
    monkeypatch.setattr(eir, "_CACHE", {})
    app = Flask(__name__)
    eir.setup_energy_routes(app)
    return app.test_client()


def _data(client, query="radius=25"):
    url = f"/api/v1/energy/site-analysis?lat={SITE[0]}&lng={SITE[1]}&state=VA"
    resp = client.get(url + (f"&{query}" if query else ""),
                      headers={"X-Internal-Key": SECRET})
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    body = resp.get_json()
    assert body["success"] is True
    return body["data"]


# ── R: the radius ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("query,radius_km,subs", [
    ("radius=25", 25.0, 2),            # js/energy-patch.js: kilometres
    ("radius=50", 50.0, 4),            # js/site-scoring-integration.js: kilometres
    ("radius=25000", 25.0, 2),         # the documented unit: metres
    ("radius=50000", 50.0, 4),         # js/land-power-enhancements.js: metres
    ("radius_km=25", 25.0, 2),
    ("", 25.0, 2),                     # the default
], ids=["25-km", "50-km", "25000-m", "50000-m", "radius_km", "default"])
def test_every_radius_form_searches_that_many_kilometres(client, query, radius_km, subs):
    data = _data(client, query)
    assert data["counts"]["substations"] == subs
    assert data["scores"]["details"]["nearestSubstationKm"] == 1.3
    assert data["radius_km"] == radius_km
    assert data["radius"] == int(radius_km * 1000)


def test_the_ashburn_request_that_came_back_empty_finds_the_substations(client):
    """The exact query string measured live on 2026-09-22."""
    data = _data(client, "radius=25")
    assert data["counts"]["substations"] == 2 and data["counts"]["powerPlants"] == 2
    assert data["scores"]["overallScore"] > 30
    assert data["radius_read"] == {"given": 25.0, "read_as": "km", "clamped": False}


def test_the_count_is_the_circle_not_the_box(client):
    """"Corner" is inside the 25 km bounding box and 28.3 km away."""
    data = _data(client, "radius=25")
    names = [s["attributes"]["NAME"] for s in data["infrastructure"]["substations"]]
    assert names == ["Ashburn 500kV", "Beaumeade"]


def test_a_radius_out_of_range_is_clamped_and_says_so(client):
    data = _data(client, "radius=0.1")
    assert data["radius_km"] == eir.RADIUS_KM_MIN and data["radius_read"]["clamped"] is True
    data = _data(client, "radius_km=5000")
    assert data["radius_km"] == eir.RADIUS_KM_MAX and data["radius_read"]["clamped"] is True


# ── U: a layer that cannot be read is unmeasured, not zero ──────────────────

def test_a_layer_that_cannot_be_read_is_null_and_scores_nothing(client, db):
    db.fail = {"substations"}
    data = _data(client)
    assert data["counts"]["substations"] is None
    cov = data["coverage"]["substations"]
    assert cov["status"] == "unavailable" and "substations" in cov["reason"]
    scores = data["scores"]
    assert scores["overallScore"] is None and scores["rating"] is None
    assert scores["powerScore"] is None
    assert set(scores["unavailable"]) == {"substations"}
    assert "nearestSubstationKm" not in scores["details"]
    # The failed statement was rolled back, so the layers after it still ran.
    assert db.rollbacks == 1
    assert data["counts"]["pipelines"] == 2 and data["counts"]["powerPlants"] == 2
    assert scores["gasScore"] == 43



def test_unread_pipelines_null_the_gas_and_overall_scores(client, db):
    db.fail = {"gas_pipelines"}
    data = _data(client)
    assert data["counts"]["pipelines"] is None
    scores = data["scores"]
    assert scores["gasScore"] is None and scores["overallScore"] is None
    assert scores["rating"] is None and scores["powerScore"] == 50
    assert not any("No gas pipelines" in r or "No active gas pipeline" in r
                   for r in scores["recommendations"])

def test_no_database_publishes_no_score(client, monkeypatch):
    def _refused():
        raise RuntimeError("connection pool exhausted")
    monkeypatch.setattr(sys.modules["main"], "get_pg_connection", _refused, raising=False)
    data = _data(client)
    for layer in ("substations", "pipelines", "powerPlants"):
        assert data["counts"][layer] is None, layer
        assert data["coverage"][layer]["status"] == "unavailable", layer
    assert data["scores"]["overallScore"] is None and data["scores"]["rating"] is None
    assert data["power_infrastructure"]["total_count"] is None
    assert data["power_infrastructure"]["total_capacity_mw"] is None


def test_an_unread_layer_is_not_cached(client, db):
    db.fail = {"power_plants_eia"}
    assert _data(client)["counts"]["powerPlants"] is None
    db.fail = set()
    again = _data(client)
    assert again["counts"]["powerPlants"] == 2 and again["scores"]["overallScore"] is not None


def test_a_layer_that_fills_the_row_cap_says_its_count_is_a_floor(client, monkeypatch):
    monkeypatch.setattr(eir, "_ROW_CAP", 2)
    data = _data(client, "radius=50")
    assert data["counts"]["substations"] == 2
    assert data["coverage"]["substations"]["count_is_floor"] is True
    assert data["coverage"]["pipelines"]["count_is_floor"] is True


def test_a_layer_read_with_nothing_in_range_is_measured_zero(client, db, tx):
    for table in db.tables:
        db.tables[table] = []
    tx.answer = (None, True)
    data = _data(client)
    assert data["counts"] == {"substations": 0, "transmissionLines": None,
                              "powerPlants": 0, "pipelines": 0}
    assert all(c["status"] == "measured" for c in data["coverage"].values())
    assert data["scores"]["overallScore"] == 30 and data["scores"]["rating"] == "Challenging"


# ── G: pipelines are measured from their points ─────────────────────────────

def test_pipelines_are_measured_from_their_points(client):
    scores = _data(client)["scores"]
    assert scores["details"]["nearestPipelineKm"] == 1.0
    assert scores["details"]["nearestPipelineOperator"] == "Columbia Gas Trans Co"
    assert scores["gasScore"] == 43                       # within 2 km, plus interstate
    assert not any("No gas pipelines found" in r for r in scores["recommendations"])


# ── T: the nearest transmission line ────────────────────────────────────────

def test_the_nearest_transmission_line_is_scored(client, tx):
    data = _data(client)
    details = data["scores"]["details"]
    assert details["nearestTransmissionKm"] == 1.6
    assert details["nearestTransmissionVoltage"] == 230.0
    assert data["scores"]["powerScore"] == 50             # 25 substation + 15 line + 10 plant
    assert tx.calls == [pytest.approx(25 / 1.609344)]
    assert data["counts"]["transmissionLines"] is None
    cov = data["coverage"]["transmissionLines"]
    assert cov["status"] == "measured" and cov["count"].startswith("not counted")


def test_a_transmission_lookup_that_did_not_run_is_unmeasured(client, tx):
    tx.answer = (None, False)
    data = _data(client)
    assert data["coverage"]["transmissionLines"]["status"] == "unavailable"
    assert data["scores"]["powerScore"] is None and data["scores"]["overallScore"] is None
    assert "nearestTransmissionKm" not in data["scores"]["details"]


def test_a_line_beyond_the_radius_is_a_measured_miss(client, tx):
    tx.answer = (dict(LINE, distance_miles=30.0), True)
    data = _data(client)
    assert data["coverage"]["transmissionLines"]["status"] == "measured"
    assert "nearestTransmissionKm" not in data["scores"]["details"]
    assert data["scores"]["powerScore"] == 35


# ── P: plants are the EIA fleet, counted past the old cap ───────────────────

def test_plants_are_the_eia_fleet_counted_past_the_old_cap(client, db):
    db.tables["power_plants_eia"] = [(f"Plant {i}", "Example Utility", 10.0, "Solar",
                                      *_at(i / 100, 0.0)) for i in range(150)]
    data = _data(client)
    assert data["counts"]["powerPlants"] == 150
    power = data["power_infrastructure"]
    assert power["total_count"] == 150
    assert power["total_capacity_mw"] == 1500.0           # every plant, not the first 20
    assert len(power["plants"]) == 20 and len(data["infrastructure"]["powerPlants"]) == 10
    assert "discovered_power_plants" not in db.statements


# ── N: a missing figure is null, not 0 ──────────────────────────────────────

def test_a_missing_figure_is_null_not_zero(client, db):
    db.tables["substations"][0] = _sub("Ashburn 500kV", 1.2, 0.5, max_volt=None)
    data = _data(client, "radius=25000")   # metres, so this test does not ride on R
    nearest = data["infrastructure"]["substations"][0]["attributes"]
    assert nearest["NAME"] == "Ashburn 500kV" and nearest["MAX_VOLT"] is None
    power = data["power_infrastructure"]
    assert power["total_generation_mwh"] is None
    rooftop = next(p for p in power["plants"] if p["name"] == "Rooftop Array")
    assert rooftop["capacity_mw"] is None and rooftop["generation_mwh"] is None
    assert power["total_capacity_mw"] == 285.0
