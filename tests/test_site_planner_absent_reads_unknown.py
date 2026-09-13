"""estimate_congestion and screen_environmental serve a reading they did not take as
Unknown, not as the lowest risk (2026-09-13).

Both producers turned an answer with no measurement in it into their most favourable
reading:

- estimate_congestion counted a COUNT that did not run as 0. execute_query returns None
  when it has no connection or the query raises, and the producer read that None as 0,
  so the failure served density_score 0 and level 'Low'. A site outside the tables'
  coverage served the same 0.
- screen_environmental screened every lookup that came back without features as 'Low':
  a point outside the US datasets' coverage, an ArcGIS error object (ArcGIS sends those
  with HTTP 200), and FEMA's answer where no flood map is in effect.

Measured on origin/main dec101190 with the real functions, the federal services live and
no database configured. Ashburn, VA and 50.363083, 9.307306 (Hesse, DE) served the same
screen: flood Unknown, wetlands Low, species Low, env_score 90, "No significant
environmental risks identified". The US site read like the German one because of the
URLs the screen queries: FWS answered {"error": {"code": 400, "message": "Invalid URL"}},
NWI {"error": {"code": 500, ...}} and FEMA's /gis/nfhl base HTTP 404, for every point.
Both sites served congestion density_score 0, level Low. composite-score served Hesse a
validated risk_resilience of 90, and compare gave both "low environmental risk, low grid
congestion" as its reasons.

  K1  a count that did not run (no connection, a query error, either count alone) serves
      level Unknown and density_score None; a count that ran keeps its value
  K2  control: counts that ran are a reading, 0 included, in a state and in a territory
  K3  outside US data coverage the estimate is Unknown and runs no query
  E1  outside US data coverage the screen is Unknown, env_score None, and sends no request
  E2  a lookup that did not answer with a query result reads Unknown, not Low
  E3  FEMA with no flood hazard area at the point reads Unknown, not Low
  E4  control: answers that measured something screen as before, in a state and in a
      territory
  S1  the scorer scores an Unknown reading exactly as it scores a missing one
  R1  analyze serves both Unknown readings, and its score breakdown says so
  R2  compare serves them and gives no low-risk reason it did not measure
  R3  composite-score declares risk_resilience unavailable when NRI and the screen both
      measured nothing
  C1  control: the recorder every test here installs catches a connection the code swallows

Every test refuses and records outbound connections at the socket layer, and fails if one
was attempted. The database is a fake connection behind the real execute_query; the
federal services are a fake requests.get returning real requests.Response objects.
"""
import inspect
import io
import json
import os
import socket

import flask
import pytest
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ASHBURN = (39.0438, -77.4874)            # Virginia
SAN_JUAN = (18.4655, -66.1057)           # Puerto Rico: the territories are inside coverage
OUTSIDE = {
    "hesse-de": (50.363083, 9.307306),   # the customer site #3880 and #3895 measured
    "toronto-on": (43.6532, -79.3832),   # inside New York's bounding box, outside the US
    "london-uk": (51.5072, -0.1276),
}

CONGESTION_KEYS = {"level", "density_score", "substations_within_radius",
                   "power_plants_within_radius", "total_generation_mw", "radius_miles"}
ENV_KEYS = {"flood_risk", "wetland_risk", "species_risk", "risks_identified", "env_score"}
NOT_CHECKED = "Not checked: FEMA flood zone, FWS critical habitat, NWI wetlands"
NOT_COVERED = NOT_CHECKED + " (their data covers only the US and its territories)"
NO_RISKS = "No significant environmental risks identified"


@pytest.fixture(autouse=True)
def attempts(monkeypatch):
    """Refuse and record every outbound connection, in every test in this file."""
    seen = []

    def resolve(host, *args, **kwargs):
        seen.append(host)
        raise OSError(f"network disabled in this test: resolve {host!r}")

    def connect(sock, address, *args, **kwargs):
        seen.append(address)
        raise OSError(f"network disabled in this test: connect {address!r}")

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket.socket, "connect", connect)
    yield seen
    assert not seen, f"the test attempted a network connection: {seen}"


@pytest.fixture
def sp():
    import site_planner

    assert os.path.samefile(site_planner.__file__, os.path.join(ROOT, "site_planner.py")), site_planner.__file__
    return site_planner


# ── the database: fake connections behind the real execute_query ─────────────────────

class _Cursor:
    """A psycopg2 cursor answering one query with a row dict, or raising an exception."""

    def __init__(self, answer):
        self._answer = answer
        self._row = None
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        if isinstance(self._answer, Exception):
            raise self._answer
        self.description = [(name,) for name in self._answer]
        self._row = tuple(self._answer.values())

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(self, answer):
        self._answer = answer

    def cursor(self):
        return _Cursor(self._answer)

    def close(self):
        pass


def _database(monkeypatch, sp, answers):
    """Answer each connection execute_query opens with the next of `answers`.

    None is no connection at all, an exception is raised by the query, and a dict is the
    row the query returns. One connection more than `answers` holds raises IndexError.
    Returns what the real execute_query returned, call by call."""
    queue = list(answers)
    returned = []

    def connect():
        answer = queue.pop(0)
        return None if answer is None else _Connection(answer)

    real = sp.execute_query

    def spy(query, params=None, fetchone=False):
        value = real(query, params, fetchone=fetchone)
        returned.append(value)
        return value

    monkeypatch.setattr(sp, "get_neon_connection", connect)
    monkeypatch.setattr(sp, "execute_query", spy)
    return returned


# ── the federal services ────────────────────────────────────────────────────────────

FEMA, FWS, NWI = "hazards.fema.gov", "USFWS_Critical_Habitat", "Wetlands/MapServer"

# What the three URLs screen_environmental queries answered on 2026-09-13, for Ashburn,
# Miami Beach, the Everglades, Hesse, London, Toronto and Mexico City alike.
FEMA_404 = (404, '<!DOCTYPE HTML PUBLIC "-//IETF//DTD HTML//EN">'
                 '<!-- Copyright (C) 2000 Tivoli Systems, Inc. -->')
FWS_INVALID_URL = (200, {"error": {"code": 400, "message": "Invalid URL", "details": ["Invalid URL"]}})
NWI_QUERY_ERROR = (200, {"error": {"code": 500, "message": "Error performing query operation",
                                   "details": []}})

NONE_HERE = (200, {"features": []})      # a query that ran and matched nothing
# FEMA's live /arcgis/rest/services base answered these for Ashburn and Miami Beach.
FEMA_X = (200, {"features": [{"attributes": {"FLD_ZONE": "X", "SFHA_TF": "F",
                                             "ZONE_SUBTY": "AREA OF MINIMAL FLOOD HAZARD"}}]})
FEMA_AE = (200, {"features": [{"attributes": {"FLD_ZONE": "AE", "SFHA_TF": "T", "ZONE_SUBTY": None}}]})
# Built from the fields the screen asks FWS and NWI for.
HABITAT = (200, {"features": [{"attributes": {"comname": "Indiana bat", "sciname": "Myotis sodalis",
                                              "status": "Final"}}]})
WETLAND = (200, {"features": [{"attributes": {"WETLAND_TYPE": "Freshwater Emergent Wetland",
                                              "ATTRIBUTE": "PEM1C"}}]})


def _response(status, body):
    """A real requests.Response carrying `body`: JSON for a dict, the text for a str."""
    response = requests.models.Response()
    response.status_code = status
    response._content = (json.dumps(body) if isinstance(body, dict) else body).encode()
    response.encoding = "utf-8"
    return response


def _federal(monkeypatch, fema, fws, nwi):
    """Answer the three lookups. Returns the lookups the screen made, in order.

    An unexpected URL raises inside requests.get, which the screen catches, so tests that
    expect the lookups to run assert the returned list as well."""
    asked = []

    def get(url, params=None, timeout=None, **kwargs):
        for key, answer in ((FEMA, fema), (FWS, fws), (NWI, nwi)):
            if key in url:
                asked.append(key)
                return _response(*answer)
        raise AssertionError(f"screen_environmental requested an unexpected URL: {url}")

    # Patch the module screen_environmental imports when it runs. A test that deletes
    # sys.modules["requests"] (tests/test_envelope_migration.py does) makes the next import a
    # new module object, so the one this file imported at collection may no longer be it.
    import requests as live_requests
    monkeypatch.setattr(live_requests, "get", get)
    return asked


NRI, AQUEDUCT = "National_Risk_Index_Counties", "aqueduct_water_risk"


def _urlopen(monkeypatch, nri, aqueduct):
    """Answer composite-score's NRI and Aqueduct queries. Returns the ones it opened."""
    import urllib.request

    opened = []

    def urlopen(req, timeout=None, **kwargs):
        url = getattr(req, "full_url", req)
        for key, payload in ((NRI, nri), (AQUEDUCT, aqueduct)):
            if key in url:
                opened.append(key)
                return io.BytesIO(json.dumps(payload).encode())
        raise AssertionError(f"composite-score opened an unexpected URL: {url}")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return opened


# ── K: estimate_congestion ──────────────────────────────────────────────────────────

QUERY_ERROR = RuntimeError("canceling statement due to statement timeout")

NOT_RUN = {
    # (substation count, plant count) answers -> (substations, plants, generation) served
    "no-connection": ((None, None), (None, None, None)),
    "query-error": ((QUERY_ERROR, QUERY_ERROR), (None, None, None)),
    "substation-count-failed": ((None, {"plant_count": 3, "total_mw": 120.4}), (None, 3, 120)),
    "plant-count-failed": (({"sub_count": 12}, QUERY_ERROR), (12, None, None)),
}


@pytest.mark.parametrize("answers, served", list(NOT_RUN.values()), ids=list(NOT_RUN))
def test_k1_a_count_that_did_not_run_serves_unknown_not_low(sp, monkeypatch, answers, served):
    returned = _database(monkeypatch, sp, answers)
    congestion = sp.estimate_congestion(*ASHBURN)

    # Both counts ran through the real execute_query, which returned None for each failure.
    assert [value is None for value in returned] == [not isinstance(a, dict) for a in answers]
    assert set(congestion) == CONGESTION_KEYS
    assert (congestion["level"], congestion["density_score"]) == ("Unknown", None)
    assert (congestion["substations_within_radius"], congestion["power_plants_within_radius"],
            congestion["total_generation_mw"]) == served


RAN = {
    # answers, site -> density_score, level
    "nothing-counted": (({"sub_count": 0}, {"plant_count": 0, "total_mw": 0}), ASHBURN, 0, "Low"),
    "moderate": (({"sub_count": 12}, {"plant_count": 10, "total_mw": 950}), ASHBURN, 56, "Moderate"),
    "high-in-a-territory": (({"sub_count": 25}, {"plant_count": 10, "total_mw": 2400}), SAN_JUAN, 95, "High"),
}


@pytest.mark.parametrize("answers, where, density, level", list(RAN.values()), ids=list(RAN))
def test_k2_control_counts_that_ran_are_a_reading_zero_included(sp, monkeypatch, answers, where, density, level):
    returned = _database(monkeypatch, sp, answers)
    congestion = sp.estimate_congestion(*where)

    assert returned == list(answers)
    assert set(congestion) == CONGESTION_KEYS
    assert (congestion["density_score"], congestion["level"]) == (density, level)
    assert congestion["substations_within_radius"] == answers[0]["sub_count"]
    assert congestion["power_plants_within_radius"] == answers[1]["plant_count"]
    assert congestion["total_generation_mw"] == answers[1]["total_mw"]


@pytest.mark.parametrize("where", list(OUTSIDE.values()), ids=list(OUTSIDE))
def test_k3_outside_us_data_coverage_the_estimate_is_unknown_and_runs_no_query(sp, monkeypatch, where):
    returned = _database(monkeypatch, sp, [{"sub_count": 0}, {"plant_count": 0, "total_mw": 0}])
    congestion = sp.estimate_congestion(*where)

    assert returned == [], "the estimate counted rows in tables that hold none here"
    assert congestion == {"level": "Unknown", "density_score": None, "substations_within_radius": None,
                          "power_plants_within_radius": None, "total_generation_mw": None,
                          "radius_miles": 15}


# ── E: screen_environmental ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("where", list(OUTSIDE.values()), ids=list(OUTSIDE))
def test_e1_outside_us_data_coverage_the_screen_is_unknown_and_sends_no_request(sp, monkeypatch, where):
    # Answered the way these services answer a point they do not cover: nothing matched.
    asked = _federal(monkeypatch, NONE_HERE, NONE_HERE, NONE_HERE)
    env = sp.screen_environmental(*where)

    assert asked == [], "the screen queried US services about a point they do not cover"
    assert env == {"flood_risk": "Unknown", "wetland_risk": "Unknown", "species_risk": "Unknown",
                   "risks_identified": [NOT_COVERED], "env_score": None}


FAILED = {
    # the category the failed lookup feeds, and what that lookup answered
    "fema-http-404-html": ("flood_risk", FEMA_404),
    "fws-error-invalid-url": ("species_risk", FWS_INVALID_URL),
    "nwi-error-query-failed": ("wetland_risk", NWI_QUERY_ERROR),
    "nwi-no-features-list": ("wetland_risk", (200, {"objectIdFieldName": "OBJECTID"})),
    "fws-http-503-with-features": ("species_risk", (503, {"features": []})),
}
MEASURED_CLEAR = {"flood_risk": FEMA_X, "species_risk": NONE_HERE, "wetland_risk": NONE_HERE}
CHECK_NAME = {"flood_risk": "FEMA flood zone", "species_risk": "FWS critical habitat",
              "wetland_risk": "NWI wetlands"}


@pytest.mark.parametrize("category, answer", list(FAILED.values()), ids=list(FAILED))
def test_e2_a_lookup_that_did_not_answer_a_query_reads_unknown_not_low(sp, monkeypatch, category, answer):
    answers = dict(MEASURED_CLEAR, **{category: answer})
    asked = _federal(monkeypatch, answers["flood_risk"], answers["species_risk"], answers["wetland_risk"])
    env = sp.screen_environmental(*ASHBURN)

    assert asked == [FEMA, FWS, NWI]
    assert set(env) == ENV_KEYS
    assert env[category] == "Unknown"
    assert all(env[other] == "Low" for other in MEASURED_CLEAR if other != category), env
    # Two of three measured still make a score, and the screen names the check it lacks
    # instead of reporting no risks.
    assert env["env_score"] == 90
    assert env["risks_identified"] == [f"Not checked: {CHECK_NAME[category]}"]


def test_e2_all_three_lookups_as_their_urls_answered_on_2026_09_13(sp, monkeypatch):
    asked = _federal(monkeypatch, FEMA_404, FWS_INVALID_URL, NWI_QUERY_ERROR)
    env = sp.screen_environmental(*ASHBURN)

    assert asked == [FEMA, FWS, NWI]
    assert env == {"flood_risk": "Unknown", "wetland_risk": "Unknown", "species_risk": "Unknown",
                   "risks_identified": [NOT_CHECKED], "env_score": None}


def test_e3_fema_with_no_flood_hazard_area_reads_unknown_not_low(sp, monkeypatch):
    asked = _federal(monkeypatch, NONE_HERE, NONE_HERE, NONE_HERE)
    env = sp.screen_environmental(*ASHBURN)

    assert asked == [FEMA, FWS, NWI]
    assert (env["flood_risk"], env["species_risk"], env["wetland_risk"]) == ("Unknown", "Low", "Low")
    assert (env["env_score"], env["risks_identified"]) == (90, ["Not checked: FEMA flood zone"])


MEASURED = {
    # site, (FEMA, FWS, NWI) answers -> (flood, species, wetland), env_score, risks_identified
    "clear": (ASHBURN, (FEMA_X, NONE_HERE, NONE_HERE), ("Low", "Low", "Low"), 100, [NO_RISKS]),
    "all-three-found": (ASHBURN, (FEMA_AE, HABITAT, WETLAND), ("High", "High", "Moderate"), 25,
                        ["FEMA Flood Zone AE (Special Flood Hazard Area)", "Critical Habitat: Indiana bat",
                         "NWI Wetlands: Freshwater Emergent Wetland within 0.6 miles"]),
    "clear-in-a-territory": (SAN_JUAN, (FEMA_X, NONE_HERE, NONE_HERE), ("Low", "Low", "Low"), 100, [NO_RISKS]),
}


@pytest.mark.parametrize("where, answers, risks, score, identified", list(MEASURED.values()), ids=list(MEASURED))
def test_e4_control_answers_that_measured_something_screen_as_before(sp, monkeypatch, where, answers, risks,
                                                                     score, identified):
    asked = _federal(monkeypatch, *answers)
    env = sp.screen_environmental(*where)

    assert asked == [FEMA, FWS, NWI]
    assert set(env) == ENV_KEYS
    assert (env["flood_risk"], env["species_risk"], env["wetland_risk"]) == risks
    assert (env["env_score"], env["risks_identified"]) == (score, identified)


# ── S: the suitability score ────────────────────────────────────────────────────────

def test_s1_the_scorer_scores_an_unknown_reading_as_it_scores_a_missing_one(sp, monkeypatch):
    returned = _database(monkeypatch, sp, [None, None])
    asked = _federal(monkeypatch, FEMA_404, FWS_INVALID_URL, NWI_QUERY_ERROR)
    congestion = sp.estimate_congestion(*ASHBURN)
    env = sp.screen_environmental(*ASHBURN)
    assert (returned, asked) == ([None, None], [FEMA, FWS, NWI])

    served = sp.compute_suitability_score([], None, None, env, congestion)["breakdown"]
    missing = sp.compute_suitability_score(
        [], None, None,
        {k: v for k, v in env.items() if k != "env_score"},
        {k: v for k, v in congestion.items() if k != "density_score"})["breakdown"]

    assert (served["environmental"]["value"], served["congestion"]["value"]) == ("Score N/A", "Unknown")
    for factor in ("environmental", "congestion"):
        assert (served[factor]["tier"], served[factor]["points"]) == \
               (missing[factor]["tier"], missing[factor]["points"]), factor
    assert served["environmental"]["tier"] != "clear" and served["congestion"]["tier"] != "low", served


# ── R: the three views that serve them ──────────────────────────────────────────────

SITE = {"lat": ASHBURN[0], "lng": ASHBURN[1], "address": "Ashburn, VA 20147", "state": "VA"}

# What every other phase of analyze, compare and composite-score that reaches a database
# or a remote service returns in these tests: nothing found.
_OTHER_PHASES = {
    "geocode_address": None, "reverse_geocode": None,
    "find_nearest_substations": [], "find_nearest_transmission": None,
    "find_nearest_transmission_measured": (None, True),  # ran, and no line is anchored there
    "get_generation_mix": {}, "find_nearby_facilities": {"count": 0, "facilities": []},
    "check_fiber_proximity": {}, "find_nearby_gas_pipelines": None,
    "find_major_pipelines": None, "get_capacity_pipeline_nearby": None,
}


@pytest.fixture
def views(sp, monkeypatch):
    """Call a registered site-planner view past its decorators. Returns (status, JSON)."""
    import routes.connectivity_score as connectivity_score

    app = flask.Flask(__name__)
    sp.register_site_planner_routes(app)
    for name, found in _OTHER_PHASES.items():
        monkeypatch.setattr(sp, name, lambda *args, _found=found, **kwargs: _found)
    monkeypatch.setattr(connectivity_score, "score_connectivity", lambda *args, **kwargs: None)

    def call(endpoint, body):
        view = inspect.unwrap(app.view_functions[endpoint])
        with app.test_request_context(method="POST", json=body):
            rv = view()
        response, status = rv if isinstance(rv, tuple) else (rv, rv.status_code)
        return status, response.get_json()

    return call


def test_r1_analyze_serves_both_unknown_readings_and_its_breakdown_says_so(sp, monkeypatch, views):
    returned = _database(monkeypatch, sp, [None, None])
    asked = _federal(monkeypatch, FEMA_404, FWS_INVALID_URL, NWI_QUERY_ERROR)
    status, body = views("site_planner_analyze", SITE)

    assert status == 200 and body["success"] is True, body
    assert (returned, asked) == ([None, None], [FEMA, FWS, NWI])
    analysis = body["analysis"]
    assert (analysis["congestion"]["level"], analysis["congestion"]["density_score"]) == ("Unknown", None)
    assert analysis["environmental"]["env_score"] is None
    breakdown = analysis["suitability_score"]["breakdown"]
    assert (breakdown["environmental"]["value"], breakdown["congestion"]["value"]) == ("Score N/A", "Unknown")
    assert breakdown["environmental"]["tier"] != "clear", breakdown


def test_r2_compare_gives_no_low_risk_reason_it_did_not_measure(sp, monkeypatch, views):
    returned = _database(monkeypatch, sp, [None, None])  # Ashburn's two counts; Hesse runs none
    asked = _federal(monkeypatch, FEMA_404, FWS_INVALID_URL, NWI_QUERY_ERROR)
    hesse = OUTSIDE["hesse-de"]
    status, body = views("site_planner_compare", {"sites": [
        {"lat": ASHBURN[0], "lng": ASHBURN[1], "state": "VA"},
        {"lat": hesse[0], "lng": hesse[1]},
    ]})

    assert status == 200 and body["success"] is True, body
    assert (returned, asked) == ([None, None], [FEMA, FWS, NWI])
    for site in body["comparison"]:
        assert (site["congestion"], site["env_score"]) == ("Unknown", None), site
        assert (site["flood_risk"], site["wetland_risk"], site["species_risk"]) == ("Unknown",) * 3, site
    reason = body["recommendation"]["reason"]
    assert "low environmental risk" not in reason and "low grid congestion" not in reason, reason


US_COUNTS = [{"sub_count": 4}, {"plant_count": 1, "total_mw": 50}]
NRI_FAILED = {"error": {"code": 504, "message": "Your request has timed out."}}
NRI_COUNTY = {"features": [{"attributes": {"RISK_SCORE": 40.0, "RISK_RATNG": "Relatively Moderate"}}]}

COMPOSITE = {
    # site, database answers, federal answers, lookups made, NRI answer -> risk_resilience served
    "outside-the-us": (OUTSIDE["hesse-de"], [], (NONE_HERE, NONE_HERE, NONE_HERE), [],
                       {"features": []}, (None, "unavailable")),
    "us-screen-unanswered-and-nri-failed": (ASHBURN, US_COUNTS, (FEMA_404, FWS_INVALID_URL, NWI_QUERY_ERROR),
                                            [FEMA, FWS, NWI], NRI_FAILED, (None, "unavailable")),
    "control-us-screen-measured-and-nri-failed": (ASHBURN, US_COUNTS, (FEMA_X, NONE_HERE, NONE_HERE),
                                                  [FEMA, FWS, NWI], NRI_FAILED, (100, "validated")),
    "control-nri-in-coverage": (ASHBURN, US_COUNTS, (FEMA_404, FWS_INVALID_URL, NWI_QUERY_ERROR),
                                [FEMA, FWS, NWI], NRI_COUNTY, (60.0, "validated")),
}


@pytest.mark.parametrize("where, counts, federal, lookups, nri, served", list(COMPOSITE.values()),
                         ids=list(COMPOSITE))
def test_r3_composite_declares_risk_resilience_unavailable_when_nothing_was_measured(
        sp, monkeypatch, views, where, counts, federal, lookups, nri, served):
    returned = _database(monkeypatch, sp, counts)
    asked = _federal(monkeypatch, *federal)
    opened = _urlopen(monkeypatch, nri, {"features": []})
    status, body = views("site_planner_composite_score", {"lat": where[0], "lng": where[1]})

    assert status == 200 and body["success"] is True, body
    assert (returned, asked, opened) == (counts, lookups, [NRI, AQUEDUCT])
    risk = body["sub_scores"]["risk_resilience"]
    assert (risk["score"], risk["coverage"]) == served, risk
    if served[1] == "unavailable":
        assert risk["basis"] == ("no FEMA NRI county score here, and the FEMA flood + FWS critical "
                                 "habitat + NWI wetlands screen measured nothing"), risk
    assert body["coverage"]["risk_resilience"] == served[1]
    assert ("risk_resilience" in body["weights_over_validated"]) == (served[1] == "validated")


# ── C: the harness ──────────────────────────────────────────────────────────────────

def test_c1_control_the_recorder_catches_a_connection_the_code_swallows(attempts):
    """Anti-vacuity for the recorder: attempts made and swallowed are still recorded."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(("192.0.2.1", 443))  # TEST-NET-1: reserved, never routed
    except OSError:
        pass
    try:
        requests.get("https://example.invalid/arcgis/rest/services/query", timeout=2)
    except requests.RequestException:
        pass

    assert len(attempts) == 2, attempts
    attempts.clear()  # the fixture fails any test that leaves an attempt recorded
