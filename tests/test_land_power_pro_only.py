"""Land & Power details are Pro, and only Pro (owner rule, 2026-09-22).

One matrix: every Land & Power REST route against every kind of caller.

  caller                                   gets
  no key, no session                       the wall: HTTP 403, no layer, cell,
                                           grade or score; Pro is the only rung
  an AI agent (the server-injected trial   the wall (this reverses the 09-22
  key is its only credential)              AI-agent preview on these routes)
  a free key, a Developer key, a $10 pack  the preview: HTTP 200, grades, verdicts,
  bought after the cutover, a session      names and counts, every figure null,
  whose access token lapsed                coordinates at two decimals, <= 3 rows,
                                           Pro the only rung; no credit spent
  a pack bought BEFORE the cutover         the full answer, one credit per answer
  a Pro key, a signed-in Pro user          the full answer
  X-Internal-Key (the MCP server), admin   the full answer, exactly as before

WHY. Before this change a keyless caller got the grade/verdict preview on the
Score and Evaluate routes and the whole answer on /api/land-power/market-
profiles; Developer opened /api/site-score; one $10-pack credit opened every
one of them; and the map's modal sold the pack and Developer as ways in.

The routes run for real, each on the module that registers it. Only the data
sources (the analysis SQL, the H3 scorer, the market-profile and snapshot
tables, grid demand, the site-score SQL) and the key ledger are stubbed.
/api/site-score is main.py's own handler, pulled out with ast and served
through the same decorator main.py puts on it (asserted separately).
"""
import ast
import base64
import copy
import hashlib
import json
import logging
import pathlib
import re
import sys
import types

import flask
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SECRET = "lp-pro-only-test-internal-key"
ADMIN = "lp-pro-only-test-admin-key-0123456789"
FREE_KEY = "dch_live_" + "f" * 32
DEV_KEY = "dch_live_" + "d" * 32
PACK_KEY = "dch_live_" + "p" * 32      # free tier, a pack bought after the cutover
OLD_PACK_KEY = "dch_live_" + "o" * 32  # free tier, a pack bought before it
PRO_KEY = "dch_live_" + "k" * 32
UNKNOWN_KEY = "dch_live_" + "u" * 32
PLANS = {FREE_KEY: "free", DEV_KEY: "developer", PACK_KEY: "free",
         OLD_PACK_KEY: "free", PRO_KEY: "pro"}
PACKS = {PACK_KEY: 0, OLD_PACK_KEY: 400}   # credits paid before the cutover

CLAUDE_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Claude/2.2553.1 Chrome/152.0.7977.76 Safari/537.36")
INJECTED = "dch_trial_" + "z" * 32

# ── the data behind each route ──────────────────────────────────────────────
ANALYSIS = {
    "site": {"lat": 39.04123, "lon": -77.48456, "state": "VA", "capacity_mw": 100.0,
             "radius_km": 25.0},
    "power": {"substations_in_radius": 10, "nearest_substation_km": 0.8,
              "est_substation_capacity_mva": 6000, "industrial_rate_cents_kwh": 8.37,
              "nearest_substations": [
                  {"name": n, "voltage_kv": 230.0, "capacity_mva": 900.0, "distance_km": d}
                  for n, d in (("Ashburn 500kV", 0.8), ("ASHBURN", 2.0), ("NIVO", 2.5),
                               ("BELMONT", 2.6), ("PACIFIC", 3.1))]},
    "fiber": {"nearest_ix": {"name": "Equinix Ashburn IX", "city": "Ashburn, VA",
                             "peak_tbps": 12.5, "distance_km": 0.8}},
    "land": {"comparable_facilities_in_radius": 685, "existing_operational_mw": 8336.0,
             "largest_nearby_mw": 500.0},
    "water": {"stress_index": 2.4},
    "tax": {"sales_tax_exempt": True, "detail": "Exempt above a $150M investment"},
    "dcpi": {"verdict": "AVOID", "excess_power_score": 37.6, "constraint_score": 47.2,
             "time_to_power_months": 31.6, "queue_capacity_mw": 31760.5},
    "feasibility_score": 76,
    "verdict": "VIABLE_SITE",
    "interactive_map_url": "https://dchub.cloud/land-power?lat=39.0412&lon=-77.4846",
    "narrative": "Site in VA for ~100 MW: feasibility score 76/100 (VIABLE_SITE).",
}
CELL = {"hex": "852aaa87fffffff", "score": 95, "grade": "A",
        "breakdown": {"power": 30, "fiber": 25, "gas": 10, "connectivity": 15, "water": 15},
        "center": {"lat": 39.03518131927748, "lng": -77.46853210994946}}
_CELLS = ["852aaa87fffffff", "852aaa97fffffff", "852aaa83fffffff",
          "852aaa8ffffffff", "852aaabbfffffff", "852aaab3fffffff", "852aa84bfffffff"]
FAKE_H3 = types.SimpleNamespace(
    latlng_to_cell=lambda lat, lng, res: _CELLS[0],
    grid_ring=lambda cell, k: list(_CELLS[1:]),
    geo_to_cells=lambda poly, res: list(_CELLS),
    polygon_to_cells=lambda poly, res: list(_CELLS),
    cell_to_boundary=lambda cell: [(38.91234, -77.61234), (38.95678, -77.55678),
                                   (38.99876, -77.60012), (38.96543, -77.65432)],
)
PROFILE_ROWS = [(m, "TX", 1200 + i, 187.345, 800 + i, 5210.77, 40 + i, None, None,
                 None, None, None, None, None, None, "2026-09-01")
                for i, m in enumerate(("Austin", "Dallas-Fort Worth", "Houston",
                                       "San Antonio", "El Paso"))]
PLANT_ROWS = [("W A Parish", "NRG Texas Power LLC", 3632.0, "coal", 29.482812, -95.631145),
              ("South Texas Project", "STP Nuclear", 2580.0, "nuclear", 28.795012, -96.048123),
              ("Martin Lake", "Luminant", 2410.0, "coal", 32.260634, -94.570611),
              ("Limestone", "NRG", 1850.0, "coal", 31.421567, -96.252123)]
SUB_ROWS = [("HARTBURG", 500.0, 500.0, 30.266668, -93.73866),
            ("CYPRESS", 500.0, 500.0, 30.303635, -94.2572)]
# /api/v1/energy/site-analysis (energy_infrastructure_routes), per table, near the site.
ENERGY_ROWS = {
    "substations": [("Ashburn 500kV", "Ashburn", "VA", "20147", "SUBSTATION", "IN SERVICE",
                     "Dominion", 500.0, 230.0, 39.0519, -77.4812)],
    "gas_pipelines": [("Line 1", "Columbia Gas Trans Co", "Interstate", 30.0, "active",
                       39.0345, -77.4752)],
    "power_plants_eia": [("Loudoun Station", "Example Utility", 285.0, "Natural Gas",
                          39.0631, -77.4502)],
}
ENERGY_LINE = ({"line_name": "PLEASANT VIEW", "voltage_kv": 230.0,
                "owner": "VIRGINIA ELECTRIC & POWER CO", "status": "IN SERVICE",
                "volt_class": None, "distance_miles": 1.0, "matched_substation": "ASHBURN"},
               True)


class _Cur:
    """Answers the market-profile, snapshot and site-score statements. A
    statement it does not recognise fails the test: a fake that answered
    everything would let a handler pass by serving nothing."""

    def __init__(self):
        self._rows, self.description = [], None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        cols = None
        if "ORDER BY POWER(lat - %s, 2)" in s:       # energy site-analysis
            rows = ENERGY_ROWS[re.search(r"FROM (\w+)", s).group(1)]
        elif "FROM market_power_profiles ORDER BY power_readiness_score" in s:
            rows = PROFILE_ROWS
        elif s.startswith("SELECT * FROM market_power_profiles WHERE market"):
            rows = [(1, "Houston", "TX")]
        elif "SELECT market FROM market_power_profiles" in s:
            rows = [("Austin",)]
        elif "FROM power_plants_eia" in s:
            rows = PLANT_ROWS
        elif "FROM substations WHERE state" in s:
            rows = SUB_ROWS
        elif "FROM facilities WHERE latitude BETWEEN" in s:
            cols = ["id", "name", "provider", "capacity_mw", "status", "lat", "lon", "slug"]
            rows = [(f"f{i}", f"Campus {i}", "Example Operator", 120.0, "Operational",
                     39.0412345 + i / 100, -77.4845678, f"campus-{i}") for i in range(5)]
        elif "FROM substations WHERE lat BETWEEN" in s:
            cols = ["id", "name", "voltage_kv", "operator", "lat", "lon"]
            rows = [(f"s{i}", f"Sub {i}", 230.0, "Dominion", 39.05, -77.49) for i in range(4)]
        elif "information_schema.columns" in s or "to_regclass" in s:
            rows = [(0,)]
        elif s.startswith("SELECT COUNT(") or "SELECT MAX(period)" in s or \
                "SELECT COUNT(DISTINCT provider)" in s:
            rows = [(7, 1234.5)]
        else:
            raise AssertionError(f"_Cur has no answer for: {s[:160]}")
        self._rows = list(rows)
        self.description = [(c,) for c in cols] if cols else None

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


TOUCHED = []   # every call into a stubbed data source, per test


class _Conn:
    def cursor(self):
        TOUCHED.append("sql")
        return _Cur()

    def rollback(self):
        pass

    def close(self):
        pass


_TREE = {}


def _main_tree():
    """main.py parsed once per session: it is ~45k lines, and every test's app
    pulls two handlers out of it."""
    if "t" not in _TREE:
        _TREE["t"] = ast.parse((ROOT / "main.py").read_text())
    return _TREE["t"]


def _auto_issue_hook(monkeypatch):
    """main.py's own before_request hook for AI-agent user agents."""
    import routes.auto_trial as at
    monkeypatch.setattr(at, "mint_trial_for_request",
                        lambda req=None, **kw: {"ok": True, "api_key": INJECTED,
                                                "expires_at": "2026-10-22T00:00:00Z"})
    tree = _main_tree()
    names = ("auto_issue_key_for_ai_agents", "_is_bulk_export_path")
    nodes = [copy.deepcopy(n) for n in tree.body
             if isinstance(n, ast.FunctionDef) and n.name in names]
    assert sorted(n.name for n in nodes) == sorted(names)
    ns = {"request": flask.request, "g": flask.g,
          "_identify_ai_platform": lambda ua: "Claude" if "Claude/" in (ua or "") else None}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "main.py", "exec"), ns)  # noqa: S102
    return ns["auto_issue_key_for_ai_agents"]


def _main_land_power_data():
    """main.py's live /api/v1/land-power/data handler and its preview, served
    through the decorator main.py puts on it (asserted separately). Only the
    grid feed and the state power prices behind it are stubbed."""
    tree = _main_tree()
    nodes = []
    for name in ("_land_power_data_preview", "land_power_consolidated"):
        found = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
        assert len(found) <= 1, name
        if found:
            node = copy.deepcopy(found[0])
            node.decorator_list = []
            nodes.append(node)
    assert nodes and nodes[-1].name == "land_power_consolidated"
    markets = [{"name": n, "lat": lat, "lng": lng, "capacity_mw": mw, "utilization": 70, "growth": 12}
               for n, lat, lng, mw in (("Northern Virginia", 39.0438, -77.4874, 4500),
                                       ("Dallas-Fort Worth", 32.7767, -96.797, 2800),
                                       ("Phoenix", 33.4484, -112.074, 1200),
                                       ("Chicago", 41.8781, -87.6298, 1800))]
    ns = {"request": flask.request, "jsonify": flask.jsonify, "logger": logging.getLogger("t"),
          "requests": None, "CAPACITY_HEATMAP_MARKETS": markets,
          "_HEATMAP_PROVENANCE": {"source": "static_fixture", "live": False},
          "gridstatus_get_load": lambda iso: TOUCHED.append("grid") or
          {"load_mw": 81234.5, "timestamp": "2026-09-22T06:00Z"}}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "main.py", "exec"), ns)  # noqa: S102
    if "_land_power_data_preview" not in ns:
        # The tree before this route joined the Land & Power gate: it carried
        # require_plan('pro'), so the matrix runs (and fails) there too.
        from api_tier_gating import require_plan
        return require_plan("pro")(ns["land_power_consolidated"])
    from util.plan_tease import lp_gated_view
    return lp_gated_view(lambda body: ns["_land_power_data_preview"](body))(ns["land_power_consolidated"])


def _main_site_score():
    """main.py's api_site_score and the helpers it calls, decorators stripped.

    Whichever of the helpers the tree defines: the matrix has to RUN on the
    tree before this change too (where the handler carried its own Developer
    gate and _honest_rest_wall), so that it fails there on its assertions
    rather than on an import."""
    from internal_auth import is_valid_internal_key
    tree = _main_tree()
    nodes = []
    for name in ("_honest_rest_wall", "_site_score_preview", "api_site_score"):
        found = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
        assert len(found) <= 1, name
        if found:
            node = copy.deepcopy(found[0])
            node.decorator_list = []
            nodes.append(node)
    assert nodes and nodes[-1].name == "api_site_score"
    ns = {"request": flask.request, "jsonify": flask.jsonify,
          "get_read_db": lambda: _Conn(), "logger": logging.getLogger("t"),
          "is_valid_internal_key": is_valid_internal_key}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "main.py", "exec"), ns)  # noqa: S102
    view = ns["api_site_score"]
    if "_site_score_preview" in ns:
        from util.plan_tease import lp_gated_view
        view = lp_gated_view(ns["_site_score_preview"])(view)
    return view


@pytest.fixture
def ledger(monkeypatch):
    import api_tier_gating
    import routes.mcp_conversion_plays as plays
    import util.location_meter as lm
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", SECRET)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", ADMIN)
    monkeypatch.delenv("DCHUB_PARTNER_EGRESS", raising=False)
    monkeypatch.setattr(api_tier_gating, "validate_api_key",
                        lambda k: {"plan": PLANS[k], "user_id": k} if k in PLANS else None)
    monkeypatch.setattr(lm, "pack_active", lambda api_key=None, **kw: api_key in PACKS)
    state = {"burns": [], "asked": []}

    def consume(key, sid, n):
        state["burns"].append((key, n))
        return {"ok": True, "remaining": 41}

    def paid_before(key, sid, cutover):
        state["asked"].append((key, cutover.isoformat()))
        return PACKS.get(key, 0)
    monkeypatch.setattr(plays, "consume_credits", consume)
    # raising=False: the matrix must also RUN (and fail on its assertions) on a
    # tree that predates this reader.
    monkeypatch.setattr(plays, "credits_paid_before", paid_before, raising=False)
    return state


@pytest.fixture
def app(ledger, monkeypatch):
    if "h3" not in sys.modules:
        try:
            import h3  # noqa: F401
        except ImportError:
            monkeypatch.setitem(sys.modules, "h3", FAKE_H3)
    import h3_scoring
    import routes.land_power_mcp as lpm
    import land_power_crawler as lpc
    import dchub_iteration_2_routes as it2
    monkeypatch.setattr(h3_scoring, "h3", FAKE_H3)
    monkeypatch.setattr(h3_scoring, "get_db", lambda: None)
    TOUCHED.clear()
    monkeypatch.setattr(h3_scoring, "score_hex_cell",
                        lambda hex_id, conn=None: TOUCHED.append("h3") or
                        {**json.loads(json.dumps(CELL)), "hex": hex_id})
    h3_scoring._h3_cache.clear()
    monkeypatch.setattr(lpm, "_build_analysis",
                        lambda *a, **k: TOUCHED.append("analysis") or json.loads(json.dumps(ANALYSIS)))
    lpm._SITE_CACHE.clear()
    monkeypatch.setattr(it2, "_get_pg_conn", lambda: _Conn())
    import routes.connectivity_score as cs      # site-score's parcel fiber read: none here
    monkeypatch.setattr(cs, "score_connectivity", lambda *a, **k: {"error": "no_db"})
    # land-power/data's answer reads state power prices, and decides its own
    # heatmap redaction from the caller's plan (routes.dcpi._dcpi_is_paid).
    monkeypatch.setitem(sys.modules, "capacity_headroom_api", types.SimpleNamespace(
        fetch_eia_retail_rate=lambda st: 8.37))
    # As the real check does, it counts Starter and Developer as paid
    # (routes/dcpi._DCPI_PAID_PLANS), so a Developer key reaches the preview
    # with the unredacted body: the preview itself has to hold the line.
    import routes.dcpi as _dcpi
    monkeypatch.setattr(_dcpi, "_dcpi_is_paid", lambda: PLANS.get(
        flask.request.headers.get("X-API-Key") or "") in ("starter", "developer", "pro"))

    a = flask.Flask("lp-pro-only")
    a.register_blueprint(lpm.land_power_mcp_bp)
    a.register_blueprint(h3_scoring.h3_bp)
    lpc.register_land_power_routes(a, lambda: _Conn(), lambda f: f)
    # /api/v1/land-power/data is main.py's own handler. (map_tier_gating also
    # defines one, but main.py never registers that module.)
    a.add_url_rule("/api/v1/land-power/data", "land_power_consolidated", _main_land_power_data())
    it2.register_iteration_2_routes(a)
    a.add_url_rule("/api/site-score", "api_site_score", _main_site_score())
    import energy_infrastructure_routes as eir      # /api/v1/energy/site-analysis
    import site_planner
    monkeypatch.setattr(sys.modules["main"], "get_pg_connection", lambda: _Conn(), raising=False)
    monkeypatch.setattr(sys.modules["main"], "return_pg_connection", lambda c: None,
                        raising=False)
    monkeypatch.setattr(site_planner, "find_nearest_transmission_measured",
                        lambda *a, **k: TOUCHED.append("transmission") or
                        copy.deepcopy(ENERGY_LINE))
    monkeypatch.setattr(eir, "_CACHE", {})
    eir.setup_energy_routes(a)
    a.before_request(_auto_issue_hook(monkeypatch))
    return a


@pytest.fixture
def client(app):
    c = app.test_client()
    c.environ_base["REMOTE_ADDR"] = "100.64.0.9"
    return c


# ── the routes, and what counts as a Land & Power detail on each ────────────
ANALYSIS_PATH = "/api/v1/land-power/site-analysis?lat=39.04123&lon=-77.48456&state=VA"
QUICK = "/api/v1/land-power/quick-score?lat=39.04123&lon=-77.48456&state=VA"
CELL_PATH = "/api/v2/scoring/h3-cell?lat=39.04&lng=-77.48&resolution=5"
HEATMAP = "/api/v2/scoring/h3-heatmap?minLat=38.9&maxLat=39.2&minLng=-77.7&maxLng=-77.3"
PROFILES = "/api/land-power/market-profiles"
PROFILE = "/api/land-power/market-profile/Houston"
LP_DATA = "/api/v1/land-power/data"
SNAPSHOT = "/api/v1/land-power/snapshot?bbox=-77.7,38.9,-77.3,39.2&layers=facilities,substations"
SITE_SCORE = "/api/site-score?lat=39.04123&lon=-77.48456&state=VA"
ENERGY = "/api/v1/energy/site-analysis?lat=39.04123&lng=-77.48456&state=VA&radius=25"
ROUTES = [ANALYSIS_PATH, QUICK, CELL_PATH, HEATMAP, PROFILES, PROFILE, LP_DATA,
          SNAPSHOT, SITE_SCORE, ENERGY]


def _details(path, body):
    """Every Land & Power figure the route's answer carries. Full: some are set.
    Preview: all are null. Never empty, or the check would be vacuous."""
    if "energy/site-analysis" in path:
        data = body.get("data") or {}
        s, power = data.get("scores") or {}, data.get("power_infrastructure") or {}
        d = s.get("details") or {}
        return ([s.get("overallScore"), s.get("powerScore"), s.get("gasScore"),
                 power.get("total_capacity_mw")]
                + [d.get(k) for k in ("nearestSubstationKm", "nearestTransmissionKm",
                                      "nearestTransmissionVoltage", "nearestPowerPlantKm",
                                      "nearestPowerPlantMW", "nearestPipelineKm")])
    if "site-analysis" in path:
        p, d = body.get("power") or {}, body.get("dcpi") or {}
        out = [body.get("feasibility_score"), p.get("nearest_substation_km"),
               p.get("est_substation_capacity_mva"), p.get("industrial_rate_cents_kwh"),
               (body.get("land") or {}).get("existing_operational_mw"),
               (body.get("water") or {}).get("stress_index"),
               d.get("excess_power_score"), d.get("queue_capacity_mw")]
        return out + [s.get(f) for s in p.get("nearest_substations") or []
                      for f in ("voltage_kv", "capacity_mva", "distance_km")]
    if "quick-score" in path:
        return [body.get("feasibility_score")]
    if "h3-cell" in path:
        cell = body.get("cell") or {}
        return ([cell.get("score")] + list((cell.get("breakdown") or {}).values())
                + [n.get("score") for n in body.get("neighbors") or []])
    if "h3-heatmap" in path:
        stats = body.get("stats") or {}
        return ([f["properties"].get(k) for f in body.get("features") or []
                 for k in ("score", "power", "fiber", "gas")]
                + [stats.get("avg_score"), stats.get("max_score")])
    if path == PROFILES:
        return [m.get(k) for m in body.get("markets") or []
                for k in ("avg_voltage_kv", "transmission_miles")]
    if path == PROFILE:
        return ([p.get("mw") for p in body.get("large_power_plants") or []]
                + [s.get("voltage_kv") for s in body.get("high_voltage_substations") or []])
    if path == LP_DATA:
        prices = body.get("energy_prices") or {}
        return ([g.get("demand_gw") for g in (body.get("grid_demand") or {}).values()]
                + [p.get("price_cents_kwh") for p in prices.values()]
                + [len(prices) or None])          # a preview carries no price rows at all
    if path == SNAPSHOT:
        subs = (body.get("layers") or {}).get("substations")
        return [s.get("voltage_kv") for s in subs] if subs else [None]
    if path.startswith("/api/site-score"):
        return ([body.get("overall_score"), body.get("power_cost")]
                + list((body.get("scores") or {}).values())
                + [(body.get("nearby") or {}).get("total_capacity_mw"),
                   (body.get("nearby") or {}).get("generation_capacity_mw")])
    raise AssertionError(path)


def _rows(path, body):
    return {"energy/site-analysis": lambda b: (
                [x for v in ((b.get("data") or {}).get("infrastructure") or {}).values()
                 for x in v]
                + ((b.get("data") or {}).get("power_infrastructure") or {}).get("plants", [])),
            "site-analysis": lambda b: (b.get("power") or {}).get("nearest_substations"),
            "h3-cell": lambda b: b.get("neighbors"),
            "h3-heatmap": lambda b: b.get("features"),
            "market-profiles": lambda b: b.get("markets"),
            "market-profile/": lambda b: b.get("large_power_plants"),
            "snapshot": lambda b: (b.get("layers") or {}).get("facilities"),
            }.get(next((k for k in ("energy/site-analysis", "site-analysis", "h3-cell",
                                    "h3-heatmap",
                                    "market-profiles", "market-profile/", "snapshot")
                        if k in path), ""), lambda b: [])(body) or []


def _token(go_url):
    assert go_url.startswith("https://dchub.cloud/go/c/"), go_url
    payload = go_url.rsplit("/", 1)[1].split(".")[0]
    return base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode().split("|")


def _assert_pro_only_ladder(body, ref=""):
    opts = body["upgrade_options"]
    assert [o["plan"] for o in opts] == ["pro"], opts
    assert _token(opts[0]["url"])[:2] == ["pro", ref]
    assert _token(body["upgrade_url"])[:2] == ["pro", ref]
    assert body["key_bound"] is bool(ref)


def _assert_no_lower_rung(raw):
    text = raw.lower()
    for gone in ("developer", "$49", "$10", "credit pack", "pack credit", "starter", "$9/",
                 '"https://dchub.cloud/pricing"'):
        assert gone not in text, gone


WALL_KEYS = {"success", "error", "_gated", "_wall", "_required_plan", "required_plan",
             "message", "free_key", "map_url", "upgrade_url", "upgrade_options", "key_bound"}


def _assert_wall(r):
    assert r.status_code == 403, (r.status_code, r.get_data(as_text=True)[:300])
    body = r.get_json()
    assert body["_wall"] is True and body["error"] == "plan_required"
    assert set(body) <= WALL_KEYS, sorted(set(body) - WALL_KEYS)   # no data key at all
    assert body["free_key"]["url"].endswith("/api/v1/keys/claim")
    _assert_pro_only_ladder(body)
    _assert_no_lower_rung(r.get_data(as_text=True))
    return body


_COORD = re.compile(r'"(?:lat|lon|lng|center_lat|center_lng)":\s*(-?\d+\.\d+)')


def _assert_preview(r, path, ref=""):
    assert r.status_code == 200, (r.status_code, r.get_data(as_text=True)[:300])
    body = r.get_json()
    assert body.get("_gated") is True and body.get("_preview_only") is True, body
    assert body["_required_plan"] == "pro" and body["_locked_fields"]
    details = _details(path, body)
    assert details and all(v is None for v in details), (path, details)
    assert len(_rows(path, body)) <= 3
    raw = r.get_data(as_text=True)
    for m in _COORD.finditer(raw):
        assert len(m.group(1).split(".")[1]) <= 2, (path, m.group(0))
    assert "no-store" in r.headers["Cache-Control"] and "private" in r.headers["Cache-Control"]
    _assert_pro_only_ladder(body, ref)
    _assert_no_lower_rung(raw)
    return body


def _assert_full(r, path):
    assert r.status_code == 200, (r.status_code, r.get_data(as_text=True)[:300])
    body = r.get_json()
    assert not body.get("_gated") and not body.get("_wall")
    assert any(v is not None for v in _details(path, body)), path
    return body


def _get(client, path, key=None, **headers):
    h = {"User-Agent": "node"}
    if key:
        h["X-API-Key"] = key
    h.update(headers)
    return client.get(path, headers=h)


def _k(key):
    return "k-" + hashlib.sha256(key.encode()).hexdigest()


# ── the matrix ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ROUTES)
def test_no_key_and_no_session_gets_the_wall(client, ledger, path):
    r = _get(client, path)
    _assert_wall(r)
    assert "public" in r.headers["Cache-Control"]      # the same for every keyless caller
    assert ledger["burns"] == [] and ledger["asked"] == []
    assert TOUCHED == [], TOUCHED                      # nothing computed for it either


def test_the_data_sources_are_really_stubbed(client):
    """Control for the TOUCHED check above: a Pro call does reach them."""
    for path in ROUTES:
        TOUCHED.clear()
        _get(client, path, PRO_KEY)
        assert TOUCHED, path


@pytest.mark.parametrize("path", ROUTES)
def test_an_ai_agent_with_no_key_gets_the_wall(client, ledger, path):
    r = client.get(path, headers={"User-Agent": CLAUDE_UA})
    _assert_wall(r)
    assert TOUCHED == [], TOUCHED
    if path.startswith("/api/v1/"):
        # the hook put its key on this response: never shared
        assert "no-store" in r.headers["Cache-Control"]
    assert ledger["burns"] == []


def test_the_hook_really_injects_on_the_v1_routes(client, app):
    """Control: without this, the AI-agent row would pass for a hook that
    never ran."""
    seen = {}

    @app.before_request
    def _peek():
        seen[flask.request.path] = flask.request.headers.get("X-API-Key")
    client.get(QUICK, headers={"User-Agent": CLAUDE_UA})
    assert seen == {"/api/v1/land-power/quick-score": INJECTED}


@pytest.mark.parametrize("key", [FREE_KEY, DEV_KEY, PACK_KEY],
                         ids=["free", "developer", "pack-after-cutover"])
@pytest.mark.parametrize("path", ROUTES)
def test_below_pro_gets_the_preview_and_spends_nothing(client, ledger, path, key):
    _assert_preview(_get(client, path, key), path, _k(key))
    assert ledger["burns"] == []


@pytest.mark.parametrize("path", ROUTES)
def test_a_session_whose_token_lapsed_gets_the_preview_not_the_wall(client, path):
    """The 90-day refresh cookie alone: the map renews the token when it sees
    `_gated` and asks again (frontend#1556). A wall would end that."""
    client.set_cookie("dchub_refresh", "opaque-refresh-token")
    _assert_preview(_get(client, path), path)


@pytest.mark.parametrize("path", [p for p in ROUTES if p.startswith("/api/v1/")])
def test_an_ai_browser_with_a_lapsed_session_gets_the_preview_not_a_401(client, path):
    """The Claude desktop browser of a signed-in user whose access token lapsed:
    the refresh cookie, no key of its own, and on /api/v1/* the trial key
    main.auto_issue_key_for_ai_agents injects (which here does not validate).
    Measured live 2026-09-22 on the owner's Pro session: the ladder answered
    access=wall while /api/v2/scoring/h3-cell, which the hook does not touch,
    answered the preview. The session is judged without the injected key."""
    client.set_cookie("dchub_refresh", "opaque-refresh-token")
    _assert_preview(client.get(path, headers={"User-Agent": CLAUDE_UA}), path)


def test_the_ladder_reads_that_browser_as_preview_too(client):
    client.set_cookie("dchub_refresh", "opaque-refresh-token")
    body = client.get(LADDER, headers={"User-Agent": CLAUDE_UA}).get_json()
    assert body["access"] == "preview" and body["key_bound"] is False


@pytest.mark.parametrize("path", ROUTES)
def test_a_signed_in_free_user_gets_the_preview(client, monkeypatch, path):
    import api_tier_gating
    monkeypatch.setattr(api_tier_gating, "_get_decode_jwt",
                        lambda: (lambda tok: {"user_id": "u-1", "email": "u@example.com"}))
    monkeypatch.setattr(api_tier_gating, "get_user_plan", lambda **k: "free")
    _assert_preview(_get(client, path, Authorization="Bearer header.payload.sig"), path)


@pytest.mark.parametrize("path", ROUTES)
def test_a_pack_bought_before_the_cutover_keeps_the_full_answer(client, ledger, path):
    r = _get(client, path, OLD_PACK_KEY)
    _assert_full(r, path)
    assert r.headers["X-DCHub-Access"] == "pack"
    assert ledger["burns"] == [(OLD_PACK_KEY, 1)]
    assert ledger["asked"] == [(OLD_PACK_KEY, "2026-09-22T06:00:00+00:00")]


@pytest.mark.parametrize("path", ROUTES)
def test_pro_gets_the_full_answer(client, ledger, path):
    r = _get(client, path, PRO_KEY)
    _assert_full(r, path)
    assert "no-store" in r.headers["Cache-Control"]
    assert ledger["burns"] == []


@pytest.mark.parametrize("path", ROUTES)
def test_a_signed_in_pro_user_gets_the_full_answer(client, monkeypatch, path):
    import api_tier_gating
    monkeypatch.setattr(api_tier_gating, "_get_decode_jwt",
                        lambda: (lambda tok: {"user_id": "u-9", "email": "p@example.com"}))
    monkeypatch.setattr(api_tier_gating, "get_user_plan", lambda **k: "pro")
    _assert_full(_get(client, path, Authorization="Bearer header.payload.sig"), path)
    client.set_cookie("dchub_token", "header.payload.sig")
    _assert_full(_get(client, path), path)


@pytest.mark.parametrize("path", ROUTES)
@pytest.mark.parametrize("hdr", [{"X-Internal-Key": SECRET}, {"X-Admin-Key": ADMIN}],
                         ids=["internal", "admin"])
def test_internal_and_admin_are_unchanged(client, ledger, path, hdr):
    _assert_full(_get(client, path, **hdr), path)
    assert ledger["burns"] == []


def test_the_map_allowance_does_not_open_site_score(client):
    """require_plan lets some of the site's own map calls through without
    resolving a caller, and /api/site-score is on that list. Not Pro."""
    _assert_preview(_get(client, SITE_SCORE, FREE_KEY, Referer="https://dchub.cloud/"),
                    SITE_SCORE, _k(FREE_KEY))


def test_an_unknown_key_is_refused_not_served(client):
    for path in ROUTES:
        assert _get(client, path, UNKNOWN_KEY).status_code == 401, path


# ── the page's ladder endpoint: Pro only, and what this caller gets ─────────

LADDER = "/api/v1/land-power/upgrade-ladder"


@pytest.mark.parametrize("key,access", [(None, "wall"), (FREE_KEY, "preview"),
                                        (DEV_KEY, "preview"), (PACK_KEY, "preview"),
                                        (OLD_PACK_KEY, "full"), (PRO_KEY, "full")])
def test_the_ladder_sells_pro_only_and_says_what_this_caller_gets(client, ledger, key, access):
    r = _get(client, LADDER, key)
    body = r.get_json()
    assert r.status_code == 200 and body["access"] == access
    assert [x["plan"] for x in body["rungs"]] == ["pro"]
    assert body["rungs"][0]["price"] == "$99/mo"
    assert _token(body["rungs"][0]["url"])[:2] == ["pro", _k(key) if key else ""]
    assert body["free_key"]["url"].endswith("/api/v1/keys/claim")
    _assert_no_lower_rung(r.get_data(as_text=True))
    assert ledger["burns"] == []                   # asking what you get costs nothing
    assert ("public" if key is None else "no-store") in r.headers["Cache-Control"]


def test_the_ladder_answers_an_ai_agent_as_keyless(client):
    body = client.get(LADDER, headers={"User-Agent": CLAUDE_UA}).get_json()
    assert body["access"] == "wall" and body["key_bound"] is False


# ── the shapes, and the wiring the harness cannot see ───────────────────────

def test_the_preview_keeps_grades_verdicts_names_and_counts(client):
    a = _get(client, ANALYSIS_PATH, FREE_KEY).get_json()
    assert a["verdict"] == "VIABLE_SITE" and a["dcpi"]["verdict"] == "AVOID"
    assert a["power"]["substations_in_radius"] == 10
    assert "credit pack" not in a["narrative"] and "Pro" in a["narrative"]
    assert not re.search(r"\d", a["narrative"].replace("DCPI", "")), a["narrative"]
    assert a["site"] == {"lat": 39.04, "lon": -77.48, "state": "VA",
                         "capacity_mw": 100.0, "radius_km": 25.0}
    assert _get(client, CELL_PATH, FREE_KEY).get_json()["cell"]["grade"] == "A"
    s = _get(client, SITE_SCORE, FREE_KEY).get_json()
    assert s["interpretation"] and s["nearby"]["substations_50km"] == 7
    m = _get(client, PROFILES, FREE_KEY).get_json()
    assert [x["market"] for x in m["markets"]] == ["Austin", "Dallas-Fort Worth", "Houston"]
    assert m["markets"][0]["substations"] == 1200 and m["_total_available"] == 5
    snap = _get(client, SNAPSHOT, FREE_KEY).get_json()
    assert "substations" not in snap["layers"] and snap["counts"]["substations"] == 4
    assert len(snap["layers"]["facilities"]) == 3



def test_the_energy_site_analysis_preview_sells_pro_to_a_keyed_caller(client):
    """/api/v1/energy/site-analysis gave a free key the full scores beside a note
    that exact detail needed "a free key or sign-in" (measured live 2026-09-22,
    with a free key). Below Pro it serves the preview, which asks a keyed caller
    for Pro and never for a key, and keeps the grade, the counts and the names."""
    r = _get(client, ENERGY, FREE_KEY)
    data = _assert_preview(r, ENERGY, _k(FREE_KEY))["data"]
    assert "free key" not in r.get_data(as_text=True).lower()
    assert data["_upgrade_cta"].endswith("come with Pro.")
    assert data["counts"]["substations"] == 1 and data["scores"]["rating"] == "Good"
    assert data["scores"]["details"]["nearestSubstationName"] == "Ashburn 500kV"
    assert data["location"] == {"lat": 39.04, "lng": -77.48}

def test_main_serves_land_power_data_through_the_land_power_gate():
    tree = _main_tree()
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
              and n.name == "land_power_consolidated")
    decos = [ast.unparse(d) for d in fn.decorator_list]
    assert decos[0].startswith("app.route('/api/v1/land-power/data'"), decos
    assert any(d.startswith("_lp_gated_view(") and "_land_power_data_preview" in d for d in decos), decos
    assert not any("require_plan" in d for d in decos), decos


def test_the_map_session_wall_on_land_power_sells_pro_only(ledger, monkeypatch):
    """free_tier_gate's map-session cap answers before the route does. On a
    Land & Power route its wall offers Pro alone; elsewhere it is unchanged."""
    import free_tier_gate as ftg
    monkeypatch.setattr("routes.email_capture.build_agent_coaching", lambda *a, **k: {}, raising=False)
    app = flask.Flask("cap")
    for path, lp in (("/api/site-score", True), ("/api/v1/land-power/data", True),
                     ("/api/v1/site-planner/composite-score", True), ("/api/v1/fiber/routes", False)):
        with app.test_request_context(path):
            resp, status = ftg._metered_402()
            body = resp.get_json()
        assert status == 402, path
        if lp:
            assert [o["plan"] for o in body["upgrade_options"]] == ["pro"], path
            assert _token(body["upgrade_url"])[0] == "pro" and "Pro" in body["message"], path
            _assert_no_lower_rung(json.dumps(body))
        else:
            assert "upgrade_options" not in body, path


def test_main_serves_site_score_through_the_land_power_gate():
    tree = _main_tree()
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "api_site_score")
    decos = [ast.unparse(d) for d in fn.decorator_list]
    assert decos[0].startswith("app.route('/api/site-score'"), decos
    assert any(d.startswith("_lp_gated_view(") and "_site_score_preview" in d for d in decos), decos
    src = ast.unparse(fn)
    assert "_honest_rest_wall" not in src and "'developer'" not in src   # the old Developer gate


def test_the_wall_is_not_rewritten_by_the_hint_middleware(app, client, monkeypatch):
    """routes/paywall_hint_middleware appends a Starter pitch to small /api/ 403s
    that carry no `_gated`. The wall carries it, so it ships as written."""
    from routes import paywall_hint_middleware as phm
    events = []
    monkeypatch.setattr(phm, "_log_ab_event", lambda *a, **k: events.append(a))
    monkeypatch.setattr(phm, "_personal_hit_pitch", lambda *a, **k: "")
    phm.register_paywall_hint_middleware(app)
    for path in (QUICK, CELL_PATH, PROFILES, SITE_SCORE):
        body = _assert_wall(_get(client, path))
        assert "_upgrade_hint" not in body
    assert events == []
