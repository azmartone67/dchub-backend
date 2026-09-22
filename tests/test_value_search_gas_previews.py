"""Keyless and below-plan callers get a preview, not the paid numbers
(2026-09-21, free/anon tighten P0).

Measured live, keyless, before this change:

  POST /api/v1/site/value                    site_value_usd_mid, $/mw_mid, the
                                             DCPI scores, time-to-power months,
                                             the state power cost and the
                                             substation capex, for any parcel
  GET  /api/v1/search/semantic               up to 50 matches, 6 dp coordinates,
                                             power_mw, the hydrated facility row
  GET  /api/v1/gas-pipelines                 50 rows, 6 dp coordinates
  GET  /api/v2/infrastructure/hifld/gas-pipelines   up to 500 rows with operators
  GET  /api/infrastructure/gas-pipelines     200 rows, every column
  GET  /api/v1/gas-pipelines-test            10 rows per point, exact coordinates

Now (util/rest_tease): X-Internal-Key and admin callers are answered exactly as
before; a plan that opens the route (Pro for the valuation, Developer for the
rest) gets the full answer marked private/no-store; a valid key below it holding
$10-pack credits gets the full answer for one credit; everyone else gets HTTP 200
with at most 3 rows, the paid numbers null, coordinates at 2 dp, and a ladder
naming only what opens the route.

Every route runs for real, registered by its own module on a bare Flask app.
Only the credential lookups, the credit ledger and each route's data source are
stubbed. The caller's plan comes from the real
api_tier_gating.get_request_principal(), the parse require_plan() uses, so a
signed-in website user resolves like an API key.
"""
import base64
import json
import pathlib
import sys

import flask
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SECRET = "value-search-gas-test-internal-key"
ADMIN = "value-search-gas-test-admin-key-0123456789"
FREE_KEY = "dch_live_" + "f" * 32
PACK_KEY = "dch_live_" + "p" * 32           # free plan, holding $10-pack credits
TRIAL_KEY = "dch_trial_" + "t" * 32
STARTER_KEY = "dch_live_" + "s" * 32
DEV_KEY = "dch_live_" + "d" * 32
PRO_KEY = "dch_live_" + "k" * 32
ENT_KEY = "dch_live_" + "e" * 32
ADMIN_ROLE_KEY = "dchub_" + "r" * 30          # an api_keys row whose role is admin
PLANS = {FREE_KEY: "free", PACK_KEY: "free", TRIAL_KEY: "identified",
         STARTER_KEY: "starter", DEV_KEY: "developer", PRO_KEY: "pro",
         ENT_KEY: "enterprise", ADMIN_ROLE_KEY: "free"}
PARTNER_IP = "104.248.242.235"
UNDECLARED_IP = "203.0.113.7"
BELOW_DEVELOPER = [None, FREE_KEY, TRIAL_KEY, STARTER_KEY]


def _plan_of(go_url):
    assert go_url.startswith("https://dchub.cloud/go/c/"), go_url
    payload = go_url.rsplit("/", 1)[1].split(".")[0]
    return base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode().split("|")


def _numbers(obj, out=None):
    """Every number anywhere in a JSON document (bools excluded)."""
    out = set() if out is None else out
    if isinstance(obj, dict):
        for v in obj.values():
            _numbers(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _numbers(v, out)
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        out.add(float(obj))
    return out


def _dp(x):
    s = repr(float(x))
    return len(s.split(".")[1].rstrip("0")) if "." in s else 0


def _assert_no_bare_pricing(body):
    text = json.dumps(body)
    assert "dchub.cloud/pricing\"" not in text and "/pricing\"" not in text, text[:400]


@pytest.fixture
def ledger(monkeypatch):
    """Key lookups, pack balances and credit burns, answered from a table."""
    import api_data_protection
    import api_tier_gating
    import routes.mcp_conversion_plays as plays
    import routes.partner_attribution as pa
    import util.location_meter as lm
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", SECRET)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", ADMIN)
    monkeypatch.delenv("DCHUB_SYNC_KEY", raising=False)
    monkeypatch.delenv("INTERNAL_WORKER_SECRET", raising=False)
    monkeypatch.delenv("DCHUB_PARTNER_EGRESS", raising=False)
    monkeypatch.setattr(api_tier_gating, "validate_api_key",
                        lambda k: {"plan": PLANS[k], "user_id": k,
                                   "role": "admin" if k == ADMIN_ROLE_KEY else "user"}
                        if k in PLANS else None)
    monkeypatch.setattr(api_data_protection, "_resolve_key_tier", lambda k: None)
    monkeypatch.setattr(lm, "pack_active", lambda api_key=None, **kw: api_key == PACK_KEY)
    state = {"burns": [], "burn_ok": True}

    def consume(key, sid, n):
        state["burns"].append((key, n))
        return {"ok": state["burn_ok"], "remaining": 41}
    monkeypatch.setattr(plays, "consume_credits", consume)
    pa._drain()
    yield state
    pa._drain()


def _headers(key=None, ip=UNDECLARED_IP, **extra):
    h = {"User-Agent": "node", "CF-Connecting-IP": ip}
    if key:
        h["X-API-Key"] = key
    h.update(extra)
    return h


# ════════════════════════════════════════════════════════════════════════════
# POST /api/v1/site/value — a Pro product
# ════════════════════════════════════════════════════════════════════════════

SITE = {"lat": 33.45, "lon": -112.07, "acres": 50, "target_mw": 100}


@pytest.fixture
def valuation(ledger, monkeypatch):
    import routes.site_valuation_engine as sve
    monkeypatch.setattr(sve, "_nearest_market", lambda lat, lon: ("phoenix", "AZ", 0.1))
    monkeypatch.setattr(sve, "_site_state", lambda lat, lon, st: ("AZ", "stub"))
    monkeypatch.setattr(sve, "_fetch_dcpi", lambda slug: {
        "verdict": "CAUTION", "verdict_subtype": "n/a", "composite_score": 43.1,
        "excess_power_score": 60.6, "constraint_score": 62.1,
        "time_to_power_months": 42.0, "iso": "WECC"})
    monkeypatch.setattr(sve, "_fetch_gas_economics",
                        lambda slug, st, lat=None: {"henry_hub_usd_mmbtu": 3.1})
    monkeypatch.setattr(sve, "_fetch_power_cost_usd_mwh",
                        lambda st: {"usd_mwh": 46.4, "sector": "IND", "source": "stub"})
    monkeypatch.setattr(sve, "_fetch_tax_abatement", lambda st: {})
    monkeypatch.setattr(sve, "_fetch_nearest_substation", lambda lat, lon: {
        "available": True, "capex_usd": 4_000_000, "miles_to_nearest": 11.2,
        "nearest_name": "TAP300430", "tier": "lateral_build"})
    monkeypatch.setattr(sve, "_fetch_live_queue_ttp", lambda iso, slug: {
        "available": True, "iso": iso, "queue_depth_mw": 91_000.0,
        "velocity_mw_yr": 12_000.0, "months_to_power": 91.0, "source": "stub"})
    monkeypatch.setattr(sve, "_fetch_comparable_sales",
                        lambda slug, st, limit=10: [{"target": "t", "value_usd": 1.2e9, "mw": 300}])
    app = flask.Flask("site-value")
    app.register_blueprint(sve.site_valuation_engine_bp)
    return app.test_client()


def _value(client, key=None, **extra):
    r = client.post("/api/v1/site/value", json=SITE, headers=_headers(key, **extra))
    return r, r.get_json()


def test_site_value_keyless_gets_a_preview_with_every_paid_number_null(valuation):
    """FAILS on the pre-change route: it handed a keyless caller the midpoint $,
    $/MW, the DCPI scores and time-to-power."""
    r, body = _value(valuation)
    assert r.status_code == 200
    assert body["_gated"] is True and body["_preview_only"] is True
    assert body["_total_available"] == 1
    assert "valuation" not in body and "scenarios" not in body
    vt = body["valuation_teaser"]
    for k in ("site_value_usd_mid", "$/mw_mid", "$/mw_uncapped",
              "$/mw_band_floor", "$/mw_band_ceiling"):
        assert vt[k] is None, k
    assert vt["site_sufficiency"]["residual_land_value"] is None
    for k in ("composite_score", "excess_power_score", "constraint_score",
              "time_to_power_months"):
        assert body["dcpi_context"][k] is None, k
    assert body["market_context"]["power_cost_usd_mwh"] is None
    sub = body["phase_3_inputs"]["substation_proximity"]
    assert sub["capex_usd"] is None and sub["miles_to_nearest"] is None
    lq = body["phase_3_inputs"]["live_queue_ttp"]
    assert lq["months_to_power"] is None and lq["queue_depth_mw"] is None
    assert all(body["scenarios_teaser"][s]["time_to_power_months"] is None
               for s in ("grid_only", "gas_btm", "gas_to_grid_hybrid"))
    # the tease: verdict, band, best-fit label
    assert body["dcpi_context"]["verdict"] == "CAUTION"
    assert body["valuation_teaser"]["$/mw_band_status"] in (
        "in_band", "ceiling_saturated", "floor_saturated")
    assert body["best_fit"]["scenario"] in ("grid_only", "gas_btm", "gas_to_grid_hybrid")
    assert "valuation_teaser.site_value_usd_mid" in body["_locked_fields"]


def test_site_value_preview_carries_no_number_the_full_answer_computed(valuation):
    """Structural, not a key list: every number the Pro answer computed from DC
    Hub data is absent from the preview. The caller's own inputs may echo."""
    _, full = _value(valuation, PRO_KEY)
    _, preview = _value(valuation)
    # site_sufficiency and power_delivery restate the caller's own acres, MW and
    # delivery schedule (acres_per_mw = acres / target_mw); they carry no DC Hub
    # data, so the preview keeps them and they are not "paid" numbers.
    val = {k: v for k, v in full["valuation"].items()
           if k not in ("site_sufficiency", "power_delivery")}
    paid = _numbers({"valuation": val,
                     **{k: full[k] for k in ("scenarios", "gas_context",
                                             "comparable_sales", "dcpi_context",
                                             "phase_3_inputs")}})
    paid.add(float(full["market_context"]["power_cost_usd_mwh"]))
    inputs = {33.45, -112.07, 50.0, 100.0, 24.0, 1.0, 0.0}
    leaked = (paid - inputs) & _numbers(preview)
    assert not leaked, sorted(leaked)[:10]


def test_site_value_ladder_names_pro_and_the_pack_only(valuation):
    _, body = _value(valuation)
    assert _plan_of(body["upgrade_url"])[0] == "pro"
    assert [o["plan"] for o in body["upgrade_options"]] == ["pro", "pack"]
    assert all(o["opens"] == "rest" for o in body["upgrade_options"])
    assert body["required_plan"] == "pro"
    assert body["upgrade_hint"]["signup_url"] == body["upgrade_url"]
    assert "stripe_url" not in body["upgrade_hint"]
    _assert_no_bare_pricing(body)


@pytest.mark.parametrize("key", [FREE_KEY, TRIAL_KEY, STARTER_KEY, DEV_KEY])
def test_site_value_keys_below_pro_get_the_preview(valuation, key):
    r, body = _value(valuation, key)
    assert r.status_code == 200 and body["_gated"] is True
    assert body["valuation_teaser"]["site_value_usd_mid"] is None


@pytest.mark.parametrize("key", [PRO_KEY, ENT_KEY])
def test_site_value_pro_and_above_get_the_full_answer_privately(valuation, ledger, key):
    r, body = _value(valuation, key)
    assert r.status_code == 200 and "_gated" not in body
    assert body["valuation"]["site_value_usd_mid"] > 0
    assert body["dcpi_context"]["composite_score"] == 43.1
    assert "private" in r.headers["Cache-Control"] and "no-store" in r.headers["Cache-Control"]
    assert ledger["burns"] == []


def test_site_value_pack_opens_one_full_valuation_per_credit(valuation, ledger):
    r, body = _value(valuation, PACK_KEY)
    assert r.status_code == 200 and body["valuation"]["site_value_usd_mid"] > 0
    assert r.headers["X-DCHub-Access"] == "pack"
    assert "no-store" in r.headers["Cache-Control"]
    assert ledger["burns"] == [(PACK_KEY, 1)]


def test_site_value_pack_burn_that_fails_gets_the_preview(valuation, ledger):
    ledger["burn_ok"] = False
    _, body = _value(valuation, PACK_KEY)
    assert body["_gated"] is True and body["valuation_teaser"]["site_value_usd_mid"] is None


@pytest.mark.parametrize("hdr", [{"X-Internal-Key": SECRET}, {"X-Admin-Key": ADMIN}])
def test_site_value_internal_and_admin_callers_are_unchanged(valuation, hdr):
    """The pre-change branch answers them: the legacy resolver reads no key here,
    so they get the numeric teaser the brain's calibration probe reads."""
    r, body = _value(valuation, **hdr)
    assert r.status_code == 200 and "_gated" not in body
    assert body["valuation_teaser"]["$/mw_mid"] > 0
    assert body["valuation_teaser"]["site_value_usd_mid"] > 0
    assert body["dcpi_context"]["composite_score"] == 43.1


def test_site_value_admin_role_key_is_unchanged(valuation, monkeypatch):
    """A key whose api_keys role is admin resolves to the 'admin' principal: the
    route answers it as before, not as a plan that happens to outrank Pro."""
    import routes.site_valuation_engine as sve
    monkeypatch.setattr(sve, "_is_pro_plus", lambda: False)   # what the legacy resolver said
    _, body = _value(valuation, ADMIN_ROLE_KEY)
    assert "_gated" not in body and "valuation" not in body
    assert body["valuation_teaser"]["site_value_usd_mid"] > 0


def test_site_value_signed_in_website_user_resolves_like_their_key(valuation, monkeypatch):
    """A Pro user browsing /sites/value carries a login JWT cookie, no key."""
    import api_tier_gating
    monkeypatch.setattr(api_tier_gating, "_decode_jwt_fn",
                        lambda t: {"user_id": 7, "email": "pro@example.com"} if t == "jwt-pro" else None)
    monkeypatch.setattr(api_tier_gating, "get_user_plan",
                        lambda user_id=None, email=None: "pro" if user_id == 7 else "free")
    valuation.set_cookie("dchub_token", "jwt-pro")
    r, body = _value(valuation)
    assert "_gated" not in body and body["valuation"]["site_value_usd_mid"] > 0


def test_sites_value_page_banner_states_what_the_route_answers(valuation):
    import routes.site_valuation_engine as sve
    dev = valuation.get("/sites/value", headers=_headers(DEV_KEY)).get_data(as_text=True)
    pro = valuation.get("/sites/value", headers=_headers(PRO_KEY)).get_data(as_text=True)
    assert "PRO + ENTERPRISE ONLY" in dev and "PRO access confirmed" not in dev
    assert "PRO access confirmed" in pro
    assert "DEVELOPER" not in sve._PRO_HERO_BANNER.split("</div>")[0]


# ════════════════════════════════════════════════════════════════════════════
# GET /api/v1/search/semantic — Developer data
# ════════════════════════════════════════════════════════════════════════════

N_MATCHES = 12


def _match(i):
    return {"id": "m%d" % i, "score": 0.9 - i / 100.0, "metadata": {
        "name": "Facility %d" % i, "provider": "Op %d" % i, "city": "Ashburn",
        "state": "VA", "country": "US", "lat": 39.0159651 + i / 1e5,
        "lng": -77.4815802 - i / 1e5, "power_mw": 50.0 + i, "status": "Operational"}}


@pytest.fixture
def search(ledger, monkeypatch):
    import dchub_iteration_3_routes as it3
    monkeypatch.setattr(it3, "_cf_creds", lambda: ("tok", "acct"))
    monkeypatch.setattr(it3, "_embed", lambda q, t, a: ([0.1] * 8, None))
    monkeypatch.setattr(it3, "_vector_query",
                        lambda vec, k, t, a: ([_match(i) for i in range(min(k, N_MATCHES))], None))

    def hydrate(ms):
        for m in ms:
            md = m["metadata"]
            m["hydrated"] = {"id": m["id"], "slug": "f-" + m["id"], "name": md["name"],
                             "latitude": md["lat"], "longitude": md["lng"],
                             "power_mw": md["power_mw"], "source_url": "https://example.org/f",
                             "raw_address": "100 Example Way"}
            m["hydration_method"] = "exact-name-provider"
        return ms
    monkeypatch.setattr(it3, "_hydrate", hydrate)
    app = flask.Flask("semantic")
    it3.register_iteration_3_routes(app)
    return app.test_client()


def _search(client, key=None, **extra):
    r = client.get("/api/v1/search/semantic?q=hyperscale&topK=50&hydrate=true&rerank=true",
                   headers=_headers(key, **extra))
    return r, r.get_json()


@pytest.mark.parametrize("key", BELOW_DEVELOPER)
def test_semantic_search_below_developer_gets_three_rounded_rows(search, key):
    """FAILS on the pre-change route: 12 matches, 7 dp, power_mw, hydrated rows."""
    r, body = _search(search, key)
    assert r.status_code == 200 and body["_gated"] is True
    assert body["count"] == len(body["matches"]) == 3
    assert body["_total_available"] == N_MATCHES
    for m in body["matches"]:
        assert m["power_mw"] is None and m["composite_score"] is None
        assert _dp(m["lat"]) <= 2 and _dp(m["lng"]) <= 2
        h = m["hydrated"]
        assert h["power_mw"] is None and _dp(h["latitude"]) <= 2
        assert "raw_address" not in h
        assert m["name"]                       # names are kept
    assert _plan_of(body["upgrade_url"])[0] == "metered"
    assert [o["plan"] for o in body["upgrade_options"]] == ["pack", "developer"]
    _assert_no_bare_pricing(body)


@pytest.mark.parametrize("key", [DEV_KEY, PRO_KEY])
def test_semantic_search_developer_and_above_get_every_match(search, ledger, key):
    r, body = _search(search, key)
    assert "_gated" not in body and body["count"] == N_MATCHES
    assert body["matches"][0]["power_mw"] is not None
    assert "no-store" in r.headers["Cache-Control"]
    assert ledger["burns"] == []


def test_semantic_search_pack_opens_the_full_list_for_one_credit(search, ledger):
    r, body = _search(search, PACK_KEY)
    assert body["count"] == N_MATCHES and r.headers["X-DCHub-Access"] == "pack"
    assert ledger["burns"] == [(PACK_KEY, 1)]


def test_semantic_search_internal_caller_is_unchanged(search):
    r, body = _search(search, **{"X-Internal-Key": SECRET})
    assert "_gated" not in body and body["count"] == N_MATCHES
    assert body["matches"][0]["lat"] == _match(0)["metadata"]["lat"]


def test_a_partner_egress_preview_is_never_shared(search):
    """Its /go/c links carry a ref minted for that one request."""
    r, body = _search(search, ip=PARTNER_IP)
    assert body["_gated"] is True
    assert "no-store" in r.headers.get("Cache-Control", "")
    refs = {_plan_of(u)[1] for u in [body["upgrade_url"]] + [o["url"] for o in body["upgrade_options"]]}
    assert len(refs) == 1 and refs.pop().startswith("a-")


# ════════════════════════════════════════════════════════════════════════════
# The gas-pipeline routes — Developer data
# ════════════════════════════════════════════════════════════════════════════

N_PIPES = 40
TABLE_TOTAL = 33_452


def _pipe(i):
    return (1000 + i, "Line %d" % i, "Example Pipeline Co %d" % i, "Interstate",
            36.0 - i / 10.0, 1500.0 + i, "active", 29.7604267 + i / 1e4,
            -95.3698028 - i / 1e4, "Houston", "TX", "US", "eia_geodot_lines")


class _Cursor:
    """Answers the SQL the gas routes send, from _pipe()."""

    def __init__(self, log, dict_rows=False):
        self.log, self.dict_rows, self._rows, self.description = log, dict_rows, [], None

    def execute(self, sql, params=None):
        self.log.append((sql, params))
        s = " ".join(sql.split())
        if s.startswith("SET "):
            self._rows = []
        elif "COUNT(DISTINCT operator)" in s:
            self._rows = [(TABLE_TOTAL, 284, 52)]
        elif s.startswith("SELECT COUNT(*)") or s.startswith("SELECT count(*)"):
            self._rows = [{"n": TABLE_TOTAL} if self.dict_rows else (TABLE_TOTAL,)]
        elif s.startswith("SELECT current_database()"):
            self._rows = [("neondb", TABLE_TOTAL)]
        else:
            n = int(params[-1]) if params else N_PIPES
            self._rows = [_pipe(i) for i in range(min(n, N_PIPES))]
            cols = ("id", "name", "operator", "pipeline_type", "diameter_inches",
                    "capacity_mcf", "status", "lat", "lng", "city", "state", "country", "source")
            if "distance_miles" in s:          # the v2 route's column list
                cols = ("name", "operator", "pipeline_type", "diameter_inches", "capacity_mcf",
                        "status", "lat", "lng", "city", "state", "distance_miles")
                self._rows = [(r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10], None)
                              for r in self._rows]
            elif "SELECT id, name, operator, lat, lng, state" in s:   # the -test route
                self._rows = [(r[0], r[1], r[2], r[7], r[8], r[10]) for r in self._rows]
                cols = ("id", "name", "operator", "lat", "lng", "state")
            self.description = [(c,) for c in cols]
            if self.dict_rows:
                full = ("id", "name", "operator", "pipeline_type", "diameter_inches",
                        "capacity_mcf", "status", "lat", "lng", "city", "state", "country",
                        "source")
                self._rows = [dict(zip(full, _pipe(i)), geom_wkt="LINESTRING(...)",
                                   created_at="2026-09-21")
                              for i in range(min(n, N_PIPES))]

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class _Conn:
    def __init__(self, log, dict_rows=False):
        self.log, self.dict_rows = log, dict_rows

    def cursor(self):
        return _Cursor(self.log, self.dict_rows)

    def close(self):
        pass

    def rollback(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def sql_log():
    return []


@pytest.fixture
def gas(ledger, monkeypatch, sql_log):
    """/api/v1/gas-pipelines and its -test twin, through the deals blueprint."""
    import psycopg2
    import routes.deals_routes as dr
    from utils.cache import BoundedCache
    monkeypatch.setattr(dr, "_require_plan", lambda plan, **kw: (lambda f: f))
    monkeypatch.setattr(dr, "_pg_connection", lambda: _Conn(sql_log))
    monkeypatch.setattr(dr, "_GAS_STATS_CACHE", BoundedCache(max_size=1, ttl=3600))
    monkeypatch.setattr(dr, "_GAS_STATS_RETRY", {"at": 0.0})
    monkeypatch.setattr(psycopg2, "connect", lambda *a, **k: _Conn(sql_log))
    monkeypatch.setenv("DATABASE_URL", "postgresql://stub")
    app = flask.Flask("gas")
    app.register_blueprint(dr.deals_bp)
    return app.test_client()


def _gas(client, key=None, path="/api/v1/gas-pipelines?lat=29.76&lng=-95.37&radius=50&limit=200",
         **extra):
    r = client.get(path, headers=_headers(key, **extra))
    return r, r.get_json()


def _assert_gas_preview(body, rows_key="pipelines", total=TABLE_TOTAL):
    rows = body[rows_key]
    assert body["_gated"] is True and body["_preview_only"] is True
    assert len(rows) == 3 and body["_total_available"] == total
    for p in rows:
        assert _dp(p["lat"]) <= 2 and _dp(p["lng"]) <= 2
        assert p["name"] is None
        for k in ("operator", "diameter_inches", "capacity_mcf"):
            assert p.get(k) is None, k
    assert _plan_of(body["upgrade_url"])[0] == "metered"
    _assert_no_bare_pricing(body)


@pytest.mark.parametrize("key", BELOW_DEVELOPER)
def test_gas_pipelines_below_developer_gets_three_rounded_rows(gas, key):
    """FAILS on the pre-change route: 40 rows (its cap was 50) at 7 dp."""
    r, body = _gas(gas, key)
    assert r.status_code == 200
    _assert_gas_preview(body)
    assert body["stats"]["total_pipelines"] == TABLE_TOTAL        # counts stay free


@pytest.mark.parametrize("key", [DEV_KEY, PRO_KEY, ENT_KEY])
def test_gas_pipelines_developer_and_above_get_every_row(gas, ledger, key):
    r, body = _gas(gas, key)
    assert "_gated" not in body and body["count"] == N_PIPES
    assert body["pipelines"][0]["operator"] == "Example Pipeline Co 0"
    assert body["pipelines"][0]["lat"] == _pipe(0)[7]
    assert "no-store" in r.headers["Cache-Control"]
    assert ledger["burns"] == []


def test_gas_pipelines_pack_opens_every_row_for_one_credit(gas, ledger):
    r, body = _gas(gas, PACK_KEY)
    assert body["count"] == N_PIPES and r.headers["X-DCHub-Access"] == "pack"
    assert ledger["burns"] == [(PACK_KEY, 1)]


def test_gas_pipelines_internal_caller_is_unchanged(gas, sql_log):
    """The legacy branch answers: the map resolver reads the internal key as
    paid (cap 500) and privileged (operator names)."""
    r, body = _gas(gas, **{"X-Internal-Key": SECRET})
    assert "_gated" not in body and body["count"] == N_PIPES
    assert body["pipelines"][0]["operator"] == "Example Pipeline Co 0"
    limits = [p[-1] for s, p in sql_log if p and "ORDER BY diameter_inches" in s]
    assert limits == [200]


def test_gas_pipelines_preview_count_uses_the_same_filters(gas, sql_log):
    _gas(gas, path="/api/v1/gas-pipelines?lat=29.76&lng=-95.37&radius=50&state=tx&type=Interstate")
    counts = [(s, p) for s, p in sql_log if s.startswith("SELECT COUNT(*) FROM gas_pipelines WHERE")]
    assert len(counts) == 1
    sql, params = counts[0]
    assert "UPPER(state) = %s" in sql and "LOWER(pipeline_type) = LOWER(%s)" in sql
    assert "lat BETWEEN %s AND %s" in sql and params[-2:] == ["TX", "Interstate"]


@pytest.mark.parametrize("key", [None, FREE_KEY])
def test_gas_pipelines_test_route_gets_the_same_preview(gas, key):
    r, body = _gas(gas, key, path="/api/v1/gas-pipelines-test?lat=29.76&lng=-95.37")
    rows = body["rows"]
    assert r.status_code == 200 and body["_gated"] is True and len(rows) == 3
    assert all(x["name"] is None and _dp(x["lat"]) <= 2 for x in rows)


def test_gas_pipelines_test_route_developer_is_unchanged(gas):
    _, body = _gas(gas, DEV_KEY, path="/api/v1/gas-pipelines-test?lat=29.76&lng=-95.37")
    assert "_gated" not in body and len(body["rows"]) == 10


@pytest.fixture
def hifld(ledger, monkeypatch, sql_log):
    import db_utils
    import expanded_infrastructure_api as xi
    monkeypatch.setattr(db_utils, "get_db", lambda *a, **k: _Conn(sql_log))
    app = flask.Flask("hifld")
    app.register_blueprint(xi.expanded_infra_bp)
    return app.test_client()


@pytest.mark.parametrize("key", [None, FREE_KEY])
def test_hifld_gas_below_developer_gets_three_rounded_rows(hifld, key):
    """FAILS on the pre-change route: up to 500 rows with operator names."""
    r, body = _gas(hifld, key, path="/api/v2/infrastructure/hifld/gas-pipelines?lat=29.76&lng=-95.37")
    assert r.status_code == 200
    _assert_gas_preview(body)
    assert all(p["distance_miles"] is None for p in body["pipelines"])


def test_hifld_gas_developer_gets_every_row(hifld):
    r, body = _gas(hifld, DEV_KEY, path="/api/v2/infrastructure/hifld/gas-pipelines?lat=29.76&lng=-95.37")
    assert "_gated" not in body and body["count"] == N_PIPES
    assert body["pipelines"][0]["operator"] == "Example Pipeline Co 0"


@pytest.fixture
def legacy_infra(ledger, monkeypatch, sql_log):
    import infrastructure_discovery as idisc

    class _Engine:
        def __init__(self):
            pass
    monkeypatch.setattr(idisc, "InfrastructureDiscoveryEngine", _Engine)
    monkeypatch.setattr(idisc, "get_db", lambda *a, **k: _Conn(sql_log, dict_rows=True))
    app = flask.Flask("legacy-infra")
    idisc.register_infrastructure_routes(app, start_scheduler=False)
    return app.test_client()


@pytest.mark.parametrize("key", [None, FREE_KEY])
def test_legacy_infra_gas_below_developer_gets_an_allowlisted_preview(legacy_infra, key):
    """FAILS on the pre-change route: 200 rows of SELECT *, every column."""
    r, body = _gas(legacy_infra, key, path="/api/infrastructure/gas-pipelines")
    assert r.status_code == 200
    _assert_gas_preview(body, rows_key="data")
    assert all("geom_wkt" not in p and "created_at" not in p for p in body["data"])


def test_legacy_infra_gas_developer_gets_every_row(legacy_infra):
    _, body = _gas(legacy_infra, DEV_KEY, path="/api/infrastructure/gas-pipelines")
    assert "_gated" not in body and body["count"] == N_PIPES
    assert body["data"][0]["operator"] == "Example Pipeline Co 0"


# ════════════════════════════════════════════════════════════════════════════
# The shared gate
# ════════════════════════════════════════════════════════════════════════════

def test_locked_fields_are_null_where_the_preview_lists_them(valuation):
    _, body = _value(valuation)
    for path in body["_locked_fields"]:
        node, parts = body, path.split(".")
        for part in parts[:-1]:
            node = node.get(part) if isinstance(node, dict) else None
        if isinstance(node, dict) and parts[-1] in node:
            assert node[parts[-1]] is None, path
