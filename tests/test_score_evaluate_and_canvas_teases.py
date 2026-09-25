"""Site Selection Canvas and the Land & Power Score/Evaluate routes answer a
keyless or free caller with the tease, and a plan that opens them with the
full answer (frontend#1536, 2026-09-21).

WHY. Measured live on 2026-09-22 before this change, keyless and cache-busted,
every one of these answered HTTP 200 with the numbers the plans sell:

  route                                   what a keyless caller got
  /api/v1/site-selection/canvas           excess, constraint, time to power and
                                          composite for up to 50 markets
  /api/v1/land-power/site-analysis        the feasibility score plus the power,
                                          land, water, tax and DCPI figures
  /api/v1/land-power/quick-score          the feasibility score
  /api/v2/scoring/h3-cell                 the Land & Power Score and breakdown
  /api/v2/scoring/h3-heatmap              the same for up to 500 cells

Now (util/plan_tease.py): a keyless or free caller gets HTTP 200 with names,
verdicts, bands and counts, at most three rows, every sold number null and
coordinates at two decimals, plus the ladder. The canvas opens at Developer,
and a valid key below it with $10-pack credits gets the full answer for one
credit. The MCP server's X-Internal-Key and the admin radar get exactly what
they got before.

★ 2026-09-22 (owner): the four Land & Power routes are Pro-only now, with a
wall for a keyless caller and no pack path. tests/test_land_power_pro_only.py
holds their whole matrix; this file keeps the canvas, and the 400s below.

The routes run for real on their own blueprints; only the data sources (the
DCPI table, the analysis SQL, the H3 scorer) and the key ledger are stubbed.
"""
import base64
import hashlib
import json
import pathlib
import re
import sys
import types

import flask
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SECRET = "teases-test-internal-key"
ADMIN = "teases-test-admin-key-0123456789abcdef"
FREE_KEY = "dch_live_" + "f" * 32
PACK_KEY = "dch_live_" + "p" * 32          # free tier, holding $10-pack credits
DEV_KEY = "dch_live_" + "d" * 32
PRO_KEY = "dch_live_" + "k" * 32
UNKNOWN_KEY = "dch_live_" + "u" * 32
PLANS = {FREE_KEY: "free", PACK_KEY: "free", DEV_KEY: "developer", PRO_KEY: "pro"}

MARKETS = [
    {"market_slug": "midland-tx", "market_name": "Midland–Odessa", "state": "TX",
     "iso": "ERCOT", "constraint_score": 20.1, "excess_power_score": 85.7,
     "time_to_power_months": 9.6, "verdict": "BUILD"},
    {"market_slug": "abilene-tx", "market_name": "Abilene", "state": "TX",
     "iso": "ERCOT", "constraint_score": 24.3, "excess_power_score": 79.2,
     "time_to_power_months": 11.4, "verdict": "BUILD"},
    {"market_slug": "amarillo-tx", "market_name": "Amarillo", "state": "TX",
     "iso": "SPP", "constraint_score": 31.7, "excess_power_score": 70.8,
     "time_to_power_months": 14.2, "verdict": "BUILD"},
    {"market_slug": "waco-tx", "market_name": "Waco", "state": "TX",
     "iso": "ERCOT", "constraint_score": 44.6, "excess_power_score": 55.5,
     "time_to_power_months": 19.8, "verdict": "CAUTION"},
    {"market_slug": "dallas-tx", "market_name": "Dallas", "state": "TX",
     "iso": "ERCOT", "constraint_score": 61.2, "excess_power_score": 41.3,
     "time_to_power_months": 28.7, "verdict": "CAUTION"},
    {"market_slug": "columbus-oh", "market_name": "Columbus", "state": "OH",
     "iso": "PJM", "constraint_score": 77.9, "excess_power_score": 18.4,
     "time_to_power_months": 41.5, "verdict": "AVOID"},
]
for _m in MARKETS:
    _m["composite_score"] = round(_m["excess_power_score"] * 0.9, 3)

ANALYSIS = {
    "site": {"lat": 39.04123, "lon": -77.48456, "state": "VA", "capacity_mw": 100.0,
             "radius_km": 25.0},
    "power": {"substations_in_radius": 10, "nearest_substation_km": 0.8,
              "est_substation_capacity_mva": 6000, "industrial_rate_cents_kwh": 8.37,
              "nearest_substations": [
                  {"name": "Ashburn 500kV", "voltage_kv": 500.0, "capacity_mva": 2000.0,
                   "distance_km": 0.8},
                  {"name": "ASHBURN", "voltage_kv": 230.0, "capacity_mva": None,
                   "distance_km": 2.0},
                  {"name": "NIVO", "voltage_kv": None, "capacity_mva": None,
                   "distance_km": 2.5},
                  {"name": "BELMONT", "voltage_kv": 230.0, "capacity_mva": None,
                   "distance_km": 2.6},
                  {"name": "PACIFIC", "voltage_kv": 230.0, "capacity_mva": None,
                   "distance_km": 3.1}]},
    "fiber": {"nearest_ix": {"name": "Equinix Ashburn IX", "city": "Ashburn, VA",
                             "lat": 39.0438, "lon": -77.4874, "participants": 850,
                             "peak_tbps": 12.5, "distance_km": 0.8}},
    "land": {"comparable_facilities_in_radius": 685, "existing_operational_mw": 8336.0,
             "largest_nearby_mw": 500.0},
    "water": {"stress_index": 2.4},
    "tax": {"sales_tax_exempt": True, "property_tax_abatement": False,
            "data_center_specific": True, "detail": "Exempt above a $150M investment",
            "status": "active", "last_verified": "2026-09-01", "source_url": "https://x"},
    "dcpi": {"verdict": "AVOID", "excess_power_score": 37.6, "constraint_score": 47.2,
             "time_to_power_months": 31.6, "queue_capacity_mw": 31760.5,
             "best_market_in_state": "Culpeper"},
    "feasibility_score": 76,
    "verdict": "VIABLE_SITE",
    "interactive_map_url": "https://dchub.cloud/land-power?lat=39.0412&lon=-77.4846&zoom=10&capacity_mw=100",
    "narrative": "Site at (39.041, -77.485) in VA for ~100 MW: feasibility score 76/100 (VIABLE_SITE).",
}

CELL = {"hex": "852aaa87fffffff", "score": 95, "grade": "A",
        "breakdown": {"power": 30, "fiber": 25, "gas": 10, "connectivity": 15, "water": 15},
        "center": {"lat": 39.03518131927748, "lng": -77.46853210994946}}

CANVAS = "/api/v1/site-selection/canvas?region=TX&verdict=ALL&limit=12"
ANALYSIS_PATH = "/api/v1/land-power/site-analysis?lat=39.04123&lon=-77.48456&state=VA"
QUICK = "/api/v1/land-power/quick-score?lat=39.04123&lon=-77.48456&state=VA"
CELL_PATH = "/api/v2/scoring/h3-cell?lat=39.04&lng=-77.48&resolution=5"
HEATMAP = "/api/v2/scoring/h3-heatmap?minLat=38.9&maxLat=39.2&minLng=-77.7&maxLng=-77.3&resolution=5"
SCORE_ROUTES = [ANALYSIS_PATH, QUICK, CELL_PATH, HEATMAP]
ALL_ROUTES = [CANVAS] + SCORE_ROUTES


@pytest.fixture
def ledger(monkeypatch):
    """Key lookups, pack balances and credit burns, answered from a table."""
    import api_tier_gating
    import routes.mcp_conversion_plays as plays
    import util.location_meter as lm
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", SECRET)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", ADMIN)
    monkeypatch.delenv("DCHUB_PARTNER_EGRESS", raising=False)
    monkeypatch.setattr(api_tier_gating, "validate_api_key",
                        lambda k: {"plan": PLANS[k], "user_id": k} if k in PLANS else None)
    monkeypatch.setattr(lm, "pack_active", lambda api_key=None, **kw: api_key == PACK_KEY)
    state = {"burns": [], "burn_ok": True}

    def consume(key, sid, n):
        state["burns"].append((key, n))
        return {"ok": state["burn_ok"], "remaining": 41}
    monkeypatch.setattr(plays, "consume_credits", consume)
    return state


_CELLS = ["852aaa87fffffff", "852aaa97fffffff", "852aaa83fffffff",
          "852aaa8ffffffff", "852aaabbfffffff", "852aaab3fffffff", "852aa84bfffffff"]
# The H3 library is not installed on the unit-test runners and none of its
# geometry is under test here, so the module gets a stand-in with the calls it
# makes. Vertices carry four or more decimals, like the real cell_to_boundary.
FAKE_H3 = types.SimpleNamespace(
    latlng_to_cell=lambda lat, lng, res: _CELLS[0],
    grid_ring=lambda cell, k: list(_CELLS[1:]),
    geo_to_cells=lambda poly, res: list(_CELLS),
    cell_to_boundary=lambda cell: [(38.91234, -77.61234), (38.95678, -77.55678),
                                   (38.99876, -77.60012), (38.96543, -77.65432),
                                   (38.92109, -77.66789), (38.90123, -77.63456)],
)


@pytest.fixture
def client(ledger, monkeypatch):
    if "h3" not in sys.modules:
        try:
            import h3  # noqa: F401
        except ImportError:
            monkeypatch.setitem(sys.modules, "h3", FAKE_H3)
    import h3_scoring
    monkeypatch.setattr(h3_scoring, "h3", FAKE_H3)
    import routes.land_power_mcp as lpm
    import routes.site_selection_canvas as ssc
    monkeypatch.setattr(ssc, "_load_markets", lambda: [dict(m) for m in MARKETS])
    monkeypatch.setattr(lpm, "_build_analysis", lambda *a, **k: json.loads(json.dumps(ANALYSIS)))
    lpm._SITE_CACHE.clear()
    monkeypatch.setattr(h3_scoring, "get_db", lambda: None)
    monkeypatch.setattr(h3_scoring, "score_hex_cell",
                        lambda hex_id, conn=None: {**json.loads(json.dumps(CELL)), "hex": hex_id})
    h3_scoring._h3_cache.clear()
    app = flask.Flask("teases")
    app.register_blueprint(ssc.site_selection_canvas_bp)
    app.register_blueprint(lpm.land_power_mcp_bp)
    app.register_blueprint(h3_scoring.h3_bp)
    c = app.test_client()
    c.environ_base["REMOTE_ADDR"] = "100.64.0.9"
    return c


def _get(client, path, key=None, **headers):
    h = {"User-Agent": "node"}
    if key:
        h["X-API-Key"] = key
    h.update(headers)
    return client.get(path, headers=h)


def _token_fields(go_url):
    assert go_url.startswith("https://dchub.cloud/go/c/"), go_url
    payload = go_url.rsplit("/", 1)[1].split(".")[0]
    return base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode().split("|")


# ── what counts as a sold number, per route ─────────────────────────────────

def _canvas_numbers(body):
    rows = list(body.get("shortlist") or [])
    rows += list((body.get("empty_result") or {}).get("excluded_top") or [])
    return [r.get(f) for r in rows for f in ("excess_power_score", "constraint_score",
                                             "time_to_power_months", "composite_score",
                                             "verdict_reasons")]


def _analysis_numbers(body):
    p, d = body.get("power") or {}, body.get("dcpi") or {}
    subs = p.get("nearest_substations") or []
    ix = (body.get("fiber") or {}).get("nearest_ix") or {}
    return ([body.get("feasibility_score"), p.get("nearest_substation_km"),
             p.get("est_substation_capacity_mva"), p.get("industrial_rate_cents_kwh"),
             (body.get("land") or {}).get("existing_operational_mw"),
             (body.get("land") or {}).get("largest_nearby_mw"),
             (body.get("water") or {}).get("stress_index"),
             (body.get("tax") or {}).get("detail"),
             d.get("excess_power_score"), d.get("constraint_score"),
             d.get("time_to_power_months"), d.get("queue_capacity_mw"),
             ix.get("distance_km"), ix.get("peak_tbps")]
            + [s.get(f) for s in subs for f in ("voltage_kv", "capacity_mva", "distance_km")])


def _numbers(path, body):
    if path.startswith("/api/v1/site-selection"):
        return _canvas_numbers(body)
    if "site-analysis" in path:
        return _analysis_numbers(body)
    if "quick-score" in path:
        return [body.get("feasibility_score")]
    if "h3-cell" in path:
        cell = body.get("cell") or {}
        return ([cell.get("score")] + list((cell.get("breakdown") or {}).values())
                + [n.get("score") for n in body.get("neighbors") or []])
    feats = body.get("features") or []
    stats = body.get("stats") or {}
    return ([f["properties"].get(k) for f in feats
             for k in ("score", "color", "power", "fiber", "gas", "connectivity", "water")]
            + [stats.get("avg_score"), stats.get("max_score"), stats.get("min_score")])


def _rows(path, body):
    if path.startswith("/api/v1/site-selection"):
        return body.get("shortlist") or []
    if "site-analysis" in path:
        return (body.get("power") or {}).get("nearest_substations") or []
    if "h3-cell" in path:
        return body.get("neighbors") or []
    if "h3-heatmap" in path:
        return body.get("features") or []
    return []


_COORD = re.compile(r'"(?:lat|lon|lng|center_lat|center_lng)":\s*(-?\d+\.\d+)')


def _assert_tease(r, path, required_plan):
    assert r.status_code == 200, r.get_data(as_text=True)[:400]
    body = r.get_json()
    assert body.get("_gated") is True and body.get("_preview_only") is True, body
    assert body["_locked_fields"] and isinstance(body["_total_available"], int)
    nums = _numbers(path, body)
    assert nums and all(v is None for v in nums), (path, nums)
    assert len(_rows(path, body)) <= 3
    raw = r.get_data(as_text=True)
    for m in _COORD.finditer(raw):
        assert len(m.group(1).split(".")[1]) <= 2, (path, m.group(0))
    for geo in re.findall(r"-?\d+\.\d{3,}", json.dumps(
            [f.get("geometry") for f in body.get("features") or []])):
        raise AssertionError(f"{path}: a polygon vertex at more than two decimals: {geo}")
    # the ladder: signed /go/c links only, no rung that does not open the route
    opts = body["upgrade_options"]
    plans = [o["plan"] for o in opts]
    assert plans == (["pack", "pro"] if required_plan == "pro"
                     else ["pack", "developer", "pro"]), plans
    assert all(o["opens"] == "rest" for o in opts)
    assert [_token_fields(o["url"])[0] for o in opts] == \
        [{"pack": "metered"}.get(p, p) for p in plans]
    lead = "pro" if required_plan == "pro" else "metered"
    assert _token_fields(body["upgrade_url"])[0] == lead
    text = raw.lower()
    for gone in ("starter", "$9/", "$199", "$299", "$699", "founding",
                 '"https://dchub.cloud/pricing"', "/ai?ref="):
        assert gone not in text, (path, gone)
    return body


# ── keyless: the tease, cacheable, links independent of the caller ──────────

@pytest.mark.parametrize("path", [CANVAS])
def test_keyless_gets_the_tease(client, ledger, path):
    r = _get(client, path)
    body = _assert_tease(r, path, "developer" if path == CANVAS else "pro")
    assert "public" in r.headers["Cache-Control"]
    refs = {_token_fields(o["url"])[1] for o in body["upgrade_options"]}
    assert refs == {""} and body["key_bound"] is False
    assert ledger["burns"] == []


def test_canvas_tease_keeps_names_verdicts_and_counts(client):
    body = _get(client, CANVAS).get_json()
    assert [r["slug"] for r in body["shortlist"]] == ["midland-tx", "abilene-tx", "amarillo-tx"]
    assert [r["verdict"] for r in body["shortlist"]] == ["BUILD", "BUILD", "BUILD"]
    assert body["matched"] == 5 and body["_total_available"] == 5
    assert body["synthesis"]["locked"] is True
    assert "verdict" not in body["synthesis"]           # the decision layer stays locked


def test_canvas_tease_does_not_sell_the_verdict_as_locked(client):
    """The page said "The verdict for <market> is one click away" while every
    row carried its verdict. The tease sells the numbers, not the verdict."""
    import routes.site_selection_canvas as ssc
    msg = _get(client, CANVAS).get_json()["synthesis"]["message"].lower()
    assert "verdicts above are free" in msg
    page = ssc._PAGE.lower()
    for false_claim in ("the verdict for '+top+' is one click away",
                        "unlock the verdict", "<li>the build / caution / avoid verdict</li>",
                        "the verdict and build plan are the paid decision layer"):
        assert false_claim not in page, false_claim
    assert "the verdicts above are free" in page


def test_canvas_tease_ignores_the_deadline_filter_and_says_so(client):
    body = _get(client, CANVAS + "&max_months=10").get_json()
    assert body["applied_filters"]["max_months"] is None
    assert body["constraint_coverage"]["max_months"]["applied"] is False
    assert body["matched"] == 5


def test_canvas_empty_result_rows_are_teased_too(client):
    body = _get(client, "/api/v1/site-selection/canvas?region=OH").get_json()
    assert body["matched"] == 0
    rows = body["empty_result"]["excluded_top"]
    assert [r["verdict"] for r in rows] == ["AVOID"]
    assert all(v is None for v in _canvas_numbers(body))


# ── free keys: the tease, private, links bound to the caller's own key ─────

@pytest.mark.parametrize("path", [CANVAS])
def test_a_free_key_gets_the_tease_with_links_bound_to_its_key(client, ledger, path):
    r = _get(client, path, FREE_KEY)
    body = _assert_tease(r, path, "developer" if path == CANVAS else "pro")
    assert "no-store" in r.headers["Cache-Control"] and "private" in r.headers["Cache-Control"]
    h = hashlib.sha256(FREE_KEY.encode()).hexdigest()
    want = {"pack": "pk-" + h, "developer": "k-" + h, "pro": "k-" + h}
    assert {o["plan"]: _token_fields(o["url"])[1] for o in body["upgrade_options"]} == \
        {p: want[p] for p in (o["plan"] for o in body["upgrade_options"])}
    assert FREE_KEY not in r.get_data(as_text=True)            # the hash, never the key
    assert body["key_bound"] is True and ledger["burns"] == []


# ── plans that open it: the full answer, private ────────────────────────────

def _assert_full(r, path):
    assert r.status_code == 200, r.get_data(as_text=True)[:400]
    body = r.get_json()
    assert not body.get("_gated")
    assert any(v is not None for v in _numbers(path, body)), path
    assert "no-store" in r.headers["Cache-Control"]
    return body


@pytest.mark.parametrize("path", [CANVAS])
def test_pro_opens_every_route(client, ledger, path):
    _assert_full(_get(client, path, PRO_KEY), path)
    assert ledger["burns"] == []


def test_developer_opens_the_canvas_with_the_decision_layer(client, ledger):
    body = _assert_full(_get(client, CANVAS, DEV_KEY), CANVAS)
    assert body["shortlist"][0]["excess_power_score"] == 85.7
    assert len(body["shortlist"]) == 5
    assert body["synthesis"].get("headline") and not body["synthesis"].get("locked")
    assert ledger["burns"] == []


@pytest.mark.parametrize("path", [CANVAS])
def test_a_pack_key_opens_it_for_one_credit(client, ledger, path):
    r = _get(client, path, PACK_KEY)
    _assert_full(r, path)
    assert r.headers["X-DCHub-Access"] == "pack"
    assert ledger["burns"] == [(PACK_KEY, 1)]


@pytest.mark.parametrize("path", [CANVAS])
def test_a_pack_that_cannot_burn_gets_the_tease_not_the_data(client, ledger, path):
    ledger["burn_ok"] = False
    _assert_tease(_get(client, path, PACK_KEY), path, "developer" if path == CANVAS else "pro")


# ── internal and admin: exactly as before ───────────────────────────────────

@pytest.mark.parametrize("path", [CANVAS])
@pytest.mark.parametrize("hdr", [{"X-Internal-Key": SECRET}, {"X-Admin-Key": ADMIN}])
def test_internal_and_admin_are_unchanged(client, ledger, path, hdr):
    r = _get(client, path, **hdr)
    assert r.status_code == 200
    body = r.get_json()
    assert not body.get("_gated")
    assert any(v is not None for v in _numbers(path, body)), path
    assert ledger["burns"] == []


def test_internal_canvas_keeps_its_old_tier_logic(client, monkeypatch):
    """The MCP server's canvas call still gets the decision layer by the tier
    tier_gate resolves for the user's key, as before this change."""
    import routes.tier_gate as tg
    monkeypatch.setattr(tg, "_resolve_caller_tier", lambda: ("FREE", {}))
    body = _get(client, CANVAS, **{"X-Internal-Key": SECRET}).get_json()
    assert body["synthesis"].get("locked") and body["tier"] == "FREE"
    assert body["shortlist"][0]["excess_power_score"] == 85.7


@pytest.mark.parametrize("key,tier,locked", [
    (PRO_KEY, "PRO", False), (DEV_KEY, "DEVELOPER", False), (FREE_KEY, "FREE", True)])
def test_a_paid_key_through_the_mcp_server_gets_the_decision_layer(client, key, tier, locked):
    """Live screen 2026-09-25: Claude with a paid key saw the recommendation
    layer locked. The MCP server's call carries the user's key next to its
    X-Internal-Key, and tier_gate resolved every dch_live_ key FREE there
    (mcp_gatekeeper knows none). The key's own plan decides now."""
    from util import mcp_key_plan
    mcp_key_plan._rest_plan_cache.clear()
    body = _get(client, CANVAS, key, **{"X-Internal-Key": SECRET}).get_json()
    assert body["tier"] == tier
    assert bool(body["synthesis"].get("locked")) is locked


def test_the_canvas_paid_set_comes_from_tier_registry():
    import routes.site_selection_canvas as ssc
    from tier_registry import paid_plan_names
    assert {n.upper() for n in paid_plan_names()} <= ssc._PAID
    assert {"TEAM", "RESEARCH_SEED", "ADMIN"} <= ssc._PAID
    assert "FREE" not in ssc._PAID and "IDENTIFIED" not in ssc._PAID


def test_an_unknown_key_is_a_401(client):
    for path in [CANVAS]:
        assert _get(client, path, UNKNOWN_KEY).status_code == 401, path


def test_the_bad_request_paths_still_400(client):
    assert client.get("/api/v2/scoring/h3-cell").status_code == 400
    assert client.get("/api/v2/scoring/h3-heatmap").status_code == 400
    assert client.get("/api/v1/land-power/site-analysis").status_code == 400


# ── an AI-agent user agent with no key (2026-09-22) ─────────────────────────
# main.auto_issue_key_for_ai_agents runs before every /api/v1/* view. For an
# AI-platform user agent with no credential it mints or reuses a dch_trial_ key
# and writes it into the request environ as X-API-Key. Measured live with the
# Claude desktop browser's user agent and no key: the canvas and quick-score
# answered 401 invalid_api_key, because the reused trial no longer validated
# and the gate read the injected key as one the caller had sent. The hook runs
# here as main.py's own code, pulled out with ast.

CLAUDE_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Claude/2.2553.1 Chrome/152.0.7977.76 Safari/537.36")
STALE_TRIAL = "dch_trial_" + "z" * 32      # what the hook hands back; it does not validate


def _auto_issue_hook(monkeypatch):
    import ast as _ast
    import copy as _copy
    import routes.auto_trial as at
    monkeypatch.setattr(at, "mint_trial_for_request",
                        lambda req=None, **kw: {"ok": True, "api_key": STALE_TRIAL,
                                                "expires_at": "2026-10-22T00:00:00Z"})
    tree = _ast.parse((ROOT / "main.py").read_text())
    names = ("auto_issue_key_for_ai_agents", "_is_bulk_export_path")
    nodes = [_copy.deepcopy(n) for n in tree.body
             if isinstance(n, _ast.FunctionDef) and n.name in names]
    assert sorted(n.name for n in nodes) == sorted(names)
    ns = {"request": flask.request, "g": flask.g,
          "_identify_ai_platform": lambda ua: "Claude" if "Claude/" in (ua or "") else None}
    exec(compile(_ast.Module(body=nodes, type_ignores=[]), "main.py", "exec"), ns)  # noqa: S102
    return ns["auto_issue_key_for_ai_agents"]


@pytest.fixture
def agent_client(client, monkeypatch):
    client.application.before_request(_auto_issue_hook(monkeypatch))
    return client


@pytest.mark.parametrize("path", [CANVAS])
def test_an_ai_agent_with_no_key_gets_the_tease_not_a_401(agent_client, ledger, path):
    r = agent_client.get(path, headers={"User-Agent": CLAUDE_UA})
    body = _assert_tease(r, path, "developer" if path == CANVAS else "pro")
    # the response is that caller's (the hook's key header rides it): never shared
    assert "no-store" in r.headers["Cache-Control"] and "private" in r.headers["Cache-Control"]
    assert {_token_fields(o["url"])[1] for o in body["upgrade_options"]} == {""}
    assert body["key_bound"] is False and ledger["burns"] == []


def test_the_hook_really_injected_the_key(agent_client):
    """Control: without this the test above would pass for a hook that never ran."""
    seen = {}

    @agent_client.application.before_request
    def _peek():
        seen["key"] = flask.request.headers.get("X-API-Key")
        seen["g"] = getattr(flask.g, "auto_issued_key", None)
    agent_client.get(CANVAS, headers={"User-Agent": CLAUDE_UA})
    assert seen == {"key": STALE_TRIAL, "g": STALE_TRIAL}


def test_an_ai_agent_that_sends_its_own_key_is_judged_on_it(agent_client, ledger):
    r = agent_client.get(CANVAS, headers={"User-Agent": CLAUDE_UA, "X-API-Key": FREE_KEY})
    body = _assert_tease(r, CANVAS, "developer")
    h = hashlib.sha256(FREE_KEY.encode()).hexdigest()
    assert _token_fields(body["upgrade_url"])[1] == "pk-" + h
    assert agent_client.get(CANVAS, headers={"User-Agent": CLAUDE_UA,
                                             "X-API-Key": UNKNOWN_KEY}).status_code == 401
    _assert_full(agent_client.get(CANVAS, headers={"User-Agent": CLAUDE_UA,
                                                   "X-API-Key": DEV_KEY}), CANVAS)
