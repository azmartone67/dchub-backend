"""Keyless routes that served paid numerics, driven route by route
(free/anon tighten, 2026-09-21).

Before this change a caller below Developer got:

  /api/v1/interconnection-queue/refined   every row with capacity_mw, exact
      lat/lng, estimated_ttp_months, fiber_km and a ready-to-pipe analyze
      handoff; the candidate resolvers (/api/v1/resolve-candidate,
      /api/v1/rank-sites, /api/v1/fiber/cluster-latency) returned the same
      frozen numbers.
  /api/v1/research/grid-intelligence      the grid payload, with no plan gate.
  /api/v1/power/totals, /api/v1/power/availability-timeline,
  /api/v1/ai-capacity-index, /api/rankings/{power,construction,states}
                                           exact MW per state / market / year.

Each route test below asserts what a keyless caller gets now, so it FAILS on
the code before this change, and asserts the paid seats (Developer, a pack
credit, the internal key) still get the full answer.

Routes are the REAL handlers: blueprints registered on a bare Flask app, and
main.py's two handlers compiled out of main.py with their gate decorators
(main.py is never imported). Only the database, the key lookup and the credit
ledger are stubbed.
"""
import ast
import datetime
import decimal
import functools
import json
import os
import pathlib
import shutil
import subprocess

import pytest
from flask import Blueprint, Flask, jsonify, request

import api_data_protection
import api_tier_gating
import routes.mcp_conversion_plays as ledger
import util.location_meter as location_meter

ROOT = pathlib.Path(__file__).resolve().parents[1]
INTERNAL = "internal-test-key-0921"
KEYS = {"k-free": {"plan": "free"}, "k-developer": {"plan": "developer"},
        "k-pro": {"plan": "pro"}, "k-free-pack": {"plan": "free"}}
KEYLESS = None
DEVELOPER = {"X-API-Key": "k-developer"}
FREE = {"X-API-Key": "k-free"}
PACK = {"X-API-Key": "k-free-pack"}
MCP = {"X-Internal-Key": INTERNAL}


@pytest.fixture
def world(monkeypatch):
    credits = {"k-free-pack": 5}
    burns = []
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", INTERNAL)
    monkeypatch.delenv("DCHUB_ADMIN_KEY", raising=False)
    monkeypatch.delenv("DCHUB_AI_WARS_KEYS", raising=False)
    monkeypatch.setenv("DCHUB_SLOW_TOOL_CACHE", "0")
    monkeypatch.setattr(api_tier_gating, "validate_api_key", lambda k: KEYS.get(k))
    monkeypatch.setattr(api_tier_gating, "_decode_jwt_fn", lambda tok: None)
    monkeypatch.setattr(api_data_protection, "_resolve_key_tier", lambda k: None)
    monkeypatch.setattr(location_meter, "pack_active",
                        lambda api_key=None, mcp_session=None: credits.get(api_key, 0) > 0)

    def consume_credits(api_key, mcp_session_id, count=1):
        burns.append(api_key)
        credits[api_key] -= count
        return {"ok": True, "remaining": credits[api_key]}

    monkeypatch.setattr(ledger, "consume_credits", consume_credits)
    return {"burns": burns}


def _call(app, path, headers=None, method="GET", body=None):
    client = app.test_client()
    if method == "POST":
        r = client.post(path, headers=headers or {}, json=body)
    else:
        r = client.get(path, headers=headers or {})
    return r, r.get_json(silent=True)


def _assert_tease_envelope(r, body):
    assert r.status_code == 200
    assert body["_gated"] is True and body["_preview_only"] is True
    assert isinstance(body["_locked_fields"], list) and body["_locked_fields"]
    assert body["upgrade_url"].startswith("https://dchub.cloud/")
    assert "no-store" not in r.headers.get("Cache-Control", "")


def _assert_full(r, body, headers=None):
    """A paid seat's answer is private/no-store; the MCP server's (internal
    key) is exactly what it was before, headers untouched by the gate."""
    assert r.status_code == 200
    assert "_gated" not in body
    if headers is MCP:
        assert "private, no-store" not in r.headers.get("Cache-Control", "")
    else:
        assert "no-store" in r.headers["Cache-Control"]


# ── the refined queue ────────────────────────────────────────────────────────

_QUEUE_COLS = ["project_name", "iso", "state", "county", "fuel_type", "capacity_mw",
               "queue_status", "queue_date", "queue_id", "poi_name", "lat", "lng",
               "fiber_km", "geo_method"]
_QUEUE_ROWS = [
    ("ATLAS COMPLEX", "CAISO", "AZ", "LA PAZ", "Battery", 3200.0, "active",
     datetime.date(2017, 5, 1), "CAISO-1402", "Delaney 500 kV", 33.991074, -114.24754,
     173.3, "county_centroid"),
    ("P2", "PJM", "VA", "LOUDOUN", "Natural Gas", 1500.0, "active",
     datetime.date(2020, 1, 1), "PJM-2", "Sub A", 39.012345, -77.498765, 12.4, "poi_exact"),
    ("P3", "PJM", "OH", "FRANKLIN", "Nuclear", 1200.0, "active",
     datetime.date(2021, 1, 1), "PJM-3", "Sub B", 39.961234, -82.998765, 30.1, "poi_exact"),
    ("P4", "ERCOT", "TX", "DALLAS", "Solar", 800.0, "active",
     datetime.date(2022, 1, 1), "ERCOT-4", "Sub C", 32.776543, -96.796987, 5.5, "poi_exact"),
    ("P5", "MISO", "IN", "MARION", "Wind", 500.0, "active",
     datetime.date(2023, 1, 1), "MISO-5", "Sub D", 39.768403, -86.158068, 44.0, "poi_exact"),
]


class _Col:
    def __init__(self, name):
        self.name = name


class _QueueCursor:
    def __init__(self, log):
        self.log, self._sql = log, ""
        self.description = [_Col(c) for c in _QUEUE_COLS]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._sql, self._params = sql, list(params or [])
        self.log.append((sql, self._params))

    def fetchall(self):
        return _QUEUE_ROWS[: int(self._params[-1])]

    def fetchone(self):
        if "COUNT(*)" in self._sql:
            return (4948, 1126273.7)
        return None      # snapshot lookup -> no candidate mint; water probe -> none


class _QueueConn:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _QueueCursor(self.log)

    def close(self):
        pass


@pytest.fixture
def queue(monkeypatch, world):
    import routes.interconnection_queues as iq
    log = []

    class _FakePsycopg:
        @staticmethod
        def connect(*a, **k):
            return _QueueConn(log)

    monkeypatch.setattr(iq, "NEON_URL", "postgresql://stub")
    monkeypatch.setattr(iq, "psycopg", _FakePsycopg)
    app = Flask(__name__)
    app.register_blueprint(iq.interconnection_queues_bp)
    return app, log


def test_refined_queue_keyless_gets_three_rows_with_the_numerics_withheld(queue):
    app, _ = queue
    r, body = _call(app, "/api/v1/interconnection-queue/refined")
    _assert_tease_envelope(r, body)
    rows = body["results"]
    assert 0 < len(rows) <= 3 and body["count_returned"] == len(rows)
    for row in rows:
        assert row["capacity_mw"] is None
        assert row["estimated_ttp_months"] is None
        assert row["fiber_km"] is None
        assert row["site_evaluation_handoff"] is None
        assert row["lat"] == round(row["lat"], 2) and row["lng"] == round(row["lng"], 2)
        assert row["representative_point"] == {"lat": row["lat"], "lng": row["lng"]}
        assert row["project_name"] and row["iso"] and row["queue_id"]
    assert body["total_queued_mw"] is None
    assert body["count_total_matching"] == 4948 == body["_total_available"]
    text = r.get_data(as_text=True)
    for exact in ("3200.0", "33.991074", "173.3", "1126273"):
        assert exact not in text


def test_refined_queue_tease_ignores_the_numeric_filters(queue):
    """The tease is computed without min_mw / max_fiber_km / max_ttp_months, so
    neither its rows nor count_total_matching depend on a withheld value."""
    app, log = queue
    r, body = _call(app, "/api/v1/interconnection-queue/refined"
                         "?iso=PJM&min_mw=1400&max_fiber_km=20&max_ttp_months=60&limit=5000")
    assert r.status_code == 200
    sql, params = next((s, p) for s, p in log if "FROM interconnect_queue" in s and "LIMIT" in s)
    assert "fiber_km <=" not in sql and 1400.0 not in params and params[-1] == 3
    assert body["filters_applied"]["min_mw"] == 0
    assert body["_preview_params_not_applied"] == ["limit", "max_fiber_km",
                                                   "max_ttp_months", "min_mw"]
    assert "isos_excluded_by_ttp" not in body


@pytest.mark.parametrize("headers", [DEVELOPER, MCP, PACK])
def test_refined_queue_paid_seats_get_the_full_answer(queue, headers):
    app, log = queue
    r, body = _call(app, "/api/v1/interconnection-queue/refined?min_mw=1000", headers)
    _assert_full(r, body, headers)
    assert [row["capacity_mw"] for row in body["results"]] == [3200.0, 1500.0, 1200.0, 800.0, 500.0]
    assert body["results"][0]["lat"] == 33.991074
    assert body["results"][0]["site_evaluation_handoff"]["analyze_site"]["capacity_mw"] == 3200.0
    assert body["total_queued_mw"] == 1126273.7
    sql, params = next((s, p) for s, p in log if "LIMIT" in s)
    assert 1000.0 in params


def test_refined_queue_pack_key_spends_one_credit_and_free_key_gets_the_tease(queue, world):
    app, _ = queue
    _call(app, "/api/v1/interconnection-queue/refined", PACK)
    assert world["burns"] == ["k-free-pack"]
    r, body = _call(app, "/api/v1/interconnection-queue/refined", FREE)
    assert body["_gated"] is True and body["results"][0]["capacity_mw"] is None
    assert world["burns"] == ["k-free-pack"]


def test_refined_queue_keeps_its_memo_under_the_gate():
    """The gate sits between the route and the memo, so the memo keeps caching
    the caller-independent full answer and never a per-caller tease."""
    src = (ROOT / "routes" / "interconnection_queues.py").read_text()
    i = src.index("def api_refined_queue(")
    head = src[src.rindex('@interconnection_queues_bp.route("/api/v1/interconnection-queue/refined")', 0, i):i]
    assert head.index("@_refined_gate") < head.index("@cache_tool_response(")


# ── candidate resolvers follow the refined queue's gate ─────────────────────

_CAND = {"candidate_id": "cand_c03ea65d34676a8d06fd", "snapshot_id": "snap_2026-09-21",
         "queue_id": "CAISO-1402", "project_name": "ATLAS COMPLEX", "iso": "CAISO",
         "state": "AZ", "county": "LA PAZ", "fuel_type": "Battery", "capacity_mw": 3200.0,
         "lat": 33.991074, "lng": -114.24754, "fiber_km": 173.3,
         "coordinate_precision": "county_centroid", "search_context": {},
         "minted_at": None, "expires_at": None}


class _Pg2Conn:
    autocommit = False

    def cursor(self):
        return object()

    def close(self):
        pass


@pytest.fixture
def candidates(monkeypatch, world):
    import psycopg2
    import routes.candidates as rc
    monkeypatch.setattr(rc, "load_candidate", lambda cur, cid: (dict(_CAND), False))
    monkeypatch.setattr(psycopg2, "connect", lambda *a, **k: _Pg2Conn())
    monkeypatch.setenv("DATABASE_URL", "postgresql://stub")
    app = Flask(__name__)
    app.register_blueprint(rc.candidates_bp)
    return app


def test_resolve_candidate_keyless_withholds_capacity_fiber_and_exact_point(candidates):
    r, body = _call(candidates, "/api/v1/resolve-candidate?candidate_id=" + _CAND["candidate_id"])
    _assert_tease_envelope(r, body)
    assert body["identity"]["capacity_mw"] is None
    assert body["location"]["fiber_km"] is None
    assert body["location"]["lat"] == 33.99 and body["location"]["lng"] == -114.25
    assert body["identity"]["project_name"] == "ATLAS COMPLEX"


@pytest.mark.parametrize("headers", [DEVELOPER, MCP])
def test_resolve_candidate_paid_seats_get_the_frozen_row(candidates, headers):
    r, body = _call(candidates, "/api/v1/resolve-candidate?candidate_id=" + _CAND["candidate_id"],
                    headers)
    _assert_full(r, body, headers)
    assert body["identity"]["capacity_mw"] == 3200.0
    assert body["location"]["lat"] == 33.991074 and body["location"]["fiber_km"] == 173.3


@pytest.fixture
def rank(monkeypatch, queue):
    import routes.candidates as rc
    monkeypatch.setattr(rc, "load_candidate", lambda cur, cid: (dict(_CAND), False))
    return queue[0]


_RANK_BODY = {"candidates": [{"candidate_id": _CAND["candidate_id"]}],
              "objectives": {"capacity_mw": 1}}


def test_rank_sites_keyless_candidate_overlay_withholds_the_frozen_numerics(rank):
    r, body = _call(rank, "/api/v1/rank-sites", method="POST", body=_RANK_BODY)
    assert r.status_code == 200
    row = body["results"][0]
    assert "capacity_mw" not in row and "fiber_km" not in row
    assert row["lat"] == 33.99 and row["lng"] == -114.25
    assert body["candidate_contract"]["locked_fields"] == ["capacity_mw", "fiber_km", "lat", "lng"]
    assert "3200" not in r.get_data(as_text=True)


def test_rank_sites_developer_candidate_overlay_is_the_frozen_row(rank):
    r, body = _call(rank, "/api/v1/rank-sites", DEVELOPER, method="POST", body=_RANK_BODY)
    row = body["results"][0]
    assert row["capacity_mw"] == 3200.0 and row["fiber_km"] == 173.3
    assert row["lat"] == 33.991074
    assert "locked_fields" not in body["candidate_contract"]


def test_cluster_latency_resolves_a_candidate_to_the_coarse_point(monkeypatch, world):
    import psycopg2
    import routes.candidates as rc
    import routes.cluster_latency as cl
    monkeypatch.setattr(rc, "load_candidate", lambda cur, cid: (dict(_CAND), False))
    monkeypatch.setattr(psycopg2, "connect", lambda *a, **k: _Pg2Conn())
    coarse, _ = cl._resolve_candidate_sites({"candidate_ids": [_CAND["candidate_id"]]}, coarse=True)
    exact, _ = cl._resolve_candidate_sites({"candidate_ids": [_CAND["candidate_id"]]})
    assert coarse.startswith("33.99,-114.25:") and exact.startswith("33.991074,-114.24754:")

    seen = []

    def spy(src, coarse=False):
        seen.append(coarse)
        return "", {"expired": [], "unknown": [], "read_failed": [], "resolved": [],
                    "snapshot_id": None}

    monkeypatch.setattr(cl, "_resolve_candidate_sites", spy)
    app = Flask(__name__)
    app.register_blueprint(cl.cluster_latency_bp)
    path = "/api/v1/fiber/cluster-latency?candidate_ids=" + _CAND["candidate_id"]
    _call(app, path)
    _call(app, path, DEVELOPER)
    assert seen == [True, False]


# ── main.py handlers, compiled out of main.py with their decorators ─────────

@functools.lru_cache(maxsize=None)
def _main_function_code(name):
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(fns) == 1, "expected exactly one top-level %s in main.py" % name
    fn = fns[0]
    # drop @app.route only; the gate decorators are what is under test
    fn.decorator_list = [d for d in fn.decorator_list
                         if not (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                                 and d.func.attr == "route")]
    return compile(ast.Module(body=[fn], type_ignores=[]), "main.py", "exec")


def _main_route(app, name, path, **extra):
    from util import paid_numeric_gate as gate
    ns = {"request": request, "jsonify": jsonify, "app": app,
          "_developer_or_pack_wall": gate.developer_or_pack_wall,
          "_tease_numerics": gate.tease_numerics, "_band_rows": gate.band_rows, **extra}
    exec(_main_function_code(name), ns)
    app.add_url_rule(path, name, ns[name])
    return app


_GRID_FULL = {"region": "PJM", "demand_mw": 96025, "peak_mw": 101716, "min_mw": 83241,
              "committed_capacity_mw": 142206.4, "demand_24h": [{"mw": 96025}]}


@pytest.fixture
def grid(world):
    seen = []

    def phase19b_grid_intelligence(region):
        seen.append((region, request.headers.get("User-Agent", "")))
        return jsonify(dict(_GRID_FULL, region=region))

    app = _main_route(Flask(__name__), "alias_research_grid_intelligence",
                      "/api/v1/research/grid-intelligence",
                      phase19b_grid_intelligence=phase19b_grid_intelligence)
    return app, seen


@pytest.mark.parametrize("headers,status", [
    (KEYLESS, 403), (FREE, 403), ({"X-API-Key": "unrecognised-key"}, 401),
    ({"User-Agent": "Mozilla/5.0 dchub-test-agent"}, 403),
])
def test_grid_alias_is_walled_below_developer(grid, headers, status):
    """The keyed-walls gate (require_plan('developer', pack_opens=True)), the
    one /api/grid/fuel-mix sits behind."""
    app, seen = grid
    r, body = _call(app, "/api/v1/research/grid-intelligence?iso=PJM", headers)
    assert r.status_code == status
    if status == 403:
        assert body["required_plan"] == "developer"
        assert body["upgrade_url"].startswith("https://dchub.cloud/")
    assert "peak_mw" not in body and "101716" not in r.get_data(as_text=True)
    assert seen == []                     # never reached the handler


@pytest.mark.parametrize("headers", [DEVELOPER, MCP, PACK])
def test_grid_alias_opens_for_developer_pack_and_internal(grid, headers, world):
    app, seen = grid
    r, body = _call(app, "/api/v1/research/grid-intelligence?iso=PJM", headers)
    _assert_full(r, body, headers)
    assert body["peak_mw"] == 101716
    # the forwarded request says the gate admitted it, so the handler's own
    # key-or-internal check serves the full view to a cookie session too
    assert seen and "dchub-alias-admitted" in seen[-1][1]
    assert world["burns"] == (["k-free-pack"] if headers is PACK else [])


class _StatesCursor:
    def execute(self, sql, params=None):
        self.sql = sql

    def fetchall(self):
        return [("", 8862, 14451.0), ("VA", 975, 17407.0), ("TX", 634, 27725.0),
                ("SP", 262, None)]


class _StatesConn:
    def cursor(self):
        return _StatesCursor()


def _states_app():
    return _main_route(Flask(__name__), "cf_stub_state_rankings", "/api/rankings/states",
                       get_pg_connection=lambda: _StatesConn(),
                       return_pg_connection=lambda c: None)


def test_state_rankings_keyless_get_bands_not_megawatts(world):
    r, body = _call(_states_app(), "/api/rankings/states")
    _assert_tease_envelope(r, body)
    va = next(row for row in body["data"] if row["state"] == "VA")
    assert va == {"state": "VA", "facility_count": 975, "total_mw": None,
                  "total_mw_band": "10–50 GW"}
    assert body["_total_available"] == 4
    assert "17407" not in r.get_data(as_text=True)


@pytest.mark.parametrize("headers", [DEVELOPER, MCP, PACK])
def test_state_rankings_paid_seats_get_megawatts(world, headers):
    r, body = _call(_states_app(), "/api/rankings/states", headers)
    _assert_full(r, body, headers)
    assert next(row for row in body["data"] if row["state"] == "VA")["total_mw"] == 17407.0


# ── rankings (routes/energy_routes.py) ───────────────────────────────────────

class _RankCursor:
    def __init__(self):
        self.description = []

    def execute(self, sql, params=None):
        self.sql = sql
        if "FROM facilities" in sql:
            self.description = [(c,) for c in ("state", "facility_count", "total_mw",
                                               "avg_mw_per_facility", "max_facility_mw",
                                               "provider_count")]

    def fetchall(self):
        D = decimal.Decimal
        if "FROM facilities" in self.sql:
            return [("TX", 435, D("73824.1"), D("519.9"), D("7650.0"), 147),
                    ("VA", 482, D("25532.0"), D("268.8"), D("8000.0"), 120)]
        return [("Meta", "Dallas", 1200, "construction"), ("Oracle", "Abilene", 1000, "construction"),
                ("QTS", "Richmond", 700, "construction"), ("Google", "Columbus", 600, "construction"),
                ("xAI", "Memphis", 1000, "construction")]

    def close(self):
        pass


class _RankConn:
    def cursor(self):
        return _RankCursor()

    def close(self):
        pass


def _rank_app():
    from routes.energy_routes import _register_rankings_routes
    bp = Blueprint("rankings_under_test", __name__)
    _register_rankings_routes(bp, get_db_connection=lambda: _RankConn())
    app = Flask(__name__)
    app.register_blueprint(bp)
    return app


def test_power_rankings_keyless_get_bands(world):
    r, body = _call(_rank_app(), "/api/rankings/power")
    _assert_tease_envelope(r, body)
    tx = body["rankings"][0]
    assert tx["state"] == "TX" and tx["rank"] == 1 and tx["facility_count"] == 435
    assert tx["total_mw"] is None and tx["total_mw_band"] == "50–100 GW"
    assert tx["avg_mw_per_facility"] is None and tx["max_facility_mw"] is None
    assert body["summary"]["total_mw"] is None and body["summary"]["total_mw_band"] == "50–100 GW"
    assert "73824" not in r.get_data(as_text=True)


def test_construction_rankings_keyless_get_bands(world):
    r, body = _call(_rank_app(), "/api/rankings/construction")
    _assert_tease_envelope(r, body)
    assert all(row["total_mw"] is None and row["total_mw_band"] for row in body["rankings"])
    assert body["summary"]["total_mw"] is None
    texas = next(row for row in body["rankings"] if row["state_name"] == "Texas")
    assert texas["total_mw_band"] == "1–5 GW" and texas["project_count"] == 2


@pytest.mark.parametrize("headers", [DEVELOPER, MCP])
def test_rankings_paid_seats_get_megawatts(world, headers):
    r, body = _call(_rank_app(), "/api/rankings/power", headers)
    _assert_full(r, body, headers)
    assert float(body["rankings"][0]["total_mw"]) == 73824.1


def test_ranking_counts_are_not_gated(world):
    """gas and fiber rank by counts, which stay free: no gate on them."""
    src = (ROOT / "routes" / "energy_routes.py").read_text()
    for path in ("/api/rankings/gas", "/api/rankings/fiber"):
        i = src.index("@rankings_bp.route('%s'" % path)
        assert "tease_numerics" not in src[i:src.index("def ", i)]


# ── power totals, the availability timeline, the AI capacity index ──────────

_TOTALS = {"operating_mw": 54000.0, "operating_count": 3000, "pipeline_mw": 61572.0,
           "pipeline_count": 207, "total_mw": 115572.0,
           "by_state": [{"state": "TX", "operating_mw": 5933.0, "pipeline_mw": 12992.0,
                         "total_mw": 18925.0, "facility_count": 634}],
           "by_iso": [],
           "top_pipeline_markets": [{"city": "Abilene", "state": "TX", "pipeline_mw": 1200.0,
                                     "project_count": 2}],
           "computed_at": "2026-09-22T03:00:00Z"}


@pytest.fixture
def totals(monkeypatch, world):
    import routes.power_totals as pt
    monkeypatch.setattr(pt, "_cached_totals", lambda: json.loads(json.dumps(_TOTALS)))
    import routes.surface_brain as sb
    monkeypatch.setattr(sb, "auto_log", lambda *a, **k: None)
    app = Flask(__name__)
    app.register_blueprint(pt.power_totals_bp)
    return app


def test_power_totals_keyless_band_states_and_markets(totals):
    r, body = _call(totals, "/api/v1/power/totals")
    _assert_tease_envelope(r, body)
    tx = body["by_state"][0]
    assert tx["total_mw"] is None and tx["total_mw_band"] == "10–50 GW"
    assert tx["operating_mw"] is None and tx["pipeline_mw_band"] == "10–50 GW"
    assert body["top_pipeline_markets"][0]["pipeline_mw"] is None
    # one national figure per key, nothing a caller can narrow: a headline stat
    assert body["total_mw"] == 115572.0 and body["operating_count"] == 3000
    assert "18925" not in r.get_data(as_text=True)


@pytest.mark.parametrize("headers", [DEVELOPER, MCP])
def test_power_totals_paid_seats_get_state_megawatts(totals, headers):
    r, body = _call(totals, "/api/v1/power/totals", headers)
    _assert_full(r, body, headers)
    assert body["by_state"][0]["total_mw"] == 18925.0


def test_power_totals_page_shows_the_same_bands(totals):
    r = totals.test_client().get("/dcpi/totals")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "10–50 GW" in html
    assert "18.9 GW" not in html and "13.0 GW" not in html and "5.9 GW" not in html


_TIMELINE = {"ok": True, "entity": "power_availability_timeline", "state": "TX",
             "timeline": [{"year": 2026, "under_construction_mw": 2640.0, "planned_mw": 900.0,
                           "testing_mw": 0.0, "other_mw": 12.0, "retiring_mw": 1500.0,
                           "units": 14, "cumulative_firm_signal_mw": 1140.0}],
             "queue_context": {"active_mw": 250000.0, "active_projects": 1800,
                               "oldest_entry": "2015-01-01"},
             "constraint_coverage": ["generation ≠ deliverable load"]}


@pytest.fixture
def timeline(monkeypatch, world):
    import routes.power_availability_timeline as pat
    monkeypatch.setattr(pat, "_CACHE", {})
    monkeypatch.setattr(pat, "_build", lambda s, y, mw: json.loads(json.dumps(_TIMELINE)))
    app = Flask(__name__)
    app.register_blueprint(pat.power_availability_timeline_bp)
    return app


def test_timeline_keyless_gets_bands(timeline):
    r, body = _call(timeline, "/api/v1/power/availability-timeline?state=TX")
    _assert_tease_envelope(r, body)
    y = body["timeline"][0]
    assert y["under_construction_mw"] is None and y["under_construction_mw_band"] == "1–5 GW"
    assert y["cumulative_firm_signal_mw"] is None and y["units"] == 14 and y["year"] == 2026
    assert body["queue_context"]["active_mw"] is None
    assert body["queue_context"]["active_mw_band"] == "100–500 GW"
    assert body["queue_context"]["active_projects"] == 1800
    assert body["constraint_coverage"] == _TIMELINE["constraint_coverage"]
    assert "2640" not in r.get_data(as_text=True)


@pytest.mark.parametrize("headers", [DEVELOPER, MCP])
def test_timeline_paid_seats_get_megawatts(timeline, headers):
    r, body = _call(timeline, "/api/v1/power/availability-timeline?state=TX", headers)
    _assert_full(r, body, headers)
    assert body["timeline"][0]["under_construction_mw"] == 2640.0


def test_timeline_bad_request_is_unchanged(timeline):
    r, body = _call(timeline, "/api/v1/power/availability-timeline")
    assert r.status_code == 400 and body["ok"] is False


_INDEX = {"index_name": "AI Compute Capacity Index", "result_count": 1,
          "computed_at": "2026-09-22T03:29:50Z",
          "markets": [{"market": "ashburn-va", "city": "Ashburn", "state": "VA", "country": "US",
                       "deployable_mw": {"value": 500.0, "note": "Estimate from market depth"},
                       "ai_ready_mw": {"value": 7200.0, "basis": "proxy", "note": "Proxy"},
                       "facility_count": 120, "metered_facility_count": 40, "tracked_count": 150,
                       "total_installed_mw": 12000.0, "pipeline_mw": 3000.0, "operator_count": 30,
                       "hyperscale_ready": True, "score": 612.5, "rank": 1, "horizon_days": 90}]}


@pytest.fixture
def index(monkeypatch, world):
    import routes.ai_capacity_index as aci
    monkeypatch.setattr(aci, "_compute_index",
                        lambda horizon, limit: (json.loads(json.dumps(_INDEX)), 200))
    app = Flask(__name__)
    app.register_blueprint(aci.ai_capacity_index_bp)
    return app


def test_ai_capacity_index_keyless_gets_bands_and_no_score(index):
    r, body = _call(index, "/api/v1/ai-capacity-index")
    _assert_tease_envelope(r, body)
    m = body["markets"][0]
    assert m["score"] is None and m["rank"] == 1 and m["facility_count"] == 120
    assert m["deployable_mw"] == {"value": None, "band": "500 MW–1 GW",
                                  "note": "Estimate from market depth"}
    assert m["ai_ready_mw"]["value"] is None and m["ai_ready_mw"]["band"] == "5–10 GW"
    assert m["total_installed_mw"] is None and m["total_installed_mw_band"] == "10–50 GW"
    assert m["pipeline_mw"] is None
    assert "612.5" not in r.get_data(as_text=True)


@pytest.mark.parametrize("headers", [DEVELOPER, MCP])
def test_ai_capacity_index_paid_seats_get_scores(index, headers):
    r, body = _call(index, "/api/v1/ai-capacity-index", headers)
    _assert_full(r, body, headers)
    assert body["markets"][0]["score"] == 612.5
    assert body["markets"][0]["deployable_mw"]["value"] == 500.0


_PAGE_HARNESS = r"""
const fs = require('fs');
const cfg = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const rows = [];
const status = {textContent: ''};
global.document = {
  querySelector: () => ({appendChild: (tr) => rows.push(tr.innerHTML)}),
  createElement: () => ({innerHTML: ''}),
  getElementById: () => status,
};
global.fetch = () => Promise.resolve({json: () => Promise.resolve(cfg.payload)});
eval(cfg.script);
setTimeout(() => { console.log(JSON.stringify({rows, status: status.textContent})); }, 50);
"""


def test_ai_capacity_page_renders_the_tease(index, tmp_path):
    """The landing page fetches the same JSON: a keyless visitor must see
    bands, not '~null MW' or 'undefined'. Runs the page's own script."""
    node = shutil.which("node")
    if node is None:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail("node is not on PATH in CI, so the page script cannot run")
        pytest.skip("node is not on PATH")
    html = index.test_client().get("/ai-capacity-index").get_data(as_text=True)
    script = html[html.rindex("<script>") + len("<script>"):html.rindex("</script>")]
    teased = index.test_client().get("/api/v1/ai-capacity-index").get_json()
    cfg = tmp_path / "page.json"
    cfg.write_text(json.dumps({"script": script, "payload": teased}))
    harness = tmp_path / "harness.cjs"
    harness.write_text(_PAGE_HARNESS)
    run = subprocess.run([node, str(harness), str(cfg)], capture_output=True, text=True,
                         timeout=60)
    assert run.returncode == 0, run.stderr[-2000:]
    out = json.loads(run.stdout.strip().splitlines()[-1])
    row = out["rows"][0]
    assert "500 MW–1 GW" in row and "10–50 GW" in row
    assert "null" not in row and "undefined" not in row
    assert "preview" in out["status"]
