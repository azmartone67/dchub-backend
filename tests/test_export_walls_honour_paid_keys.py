"""A paying key opens the export it bought, whichever channel it arrives on.

NO NETWORK. psycopg2.connect is faked: the fake answers only the lookups it
recognises by their SQL (mcp_dev_keys by api_key, users by id or email), so
api_tier_gating.validate_api_key, get_user_plan and request_plan_ceiling,
util/mcp_key_plan and routes/tier_gate run as written. The logins are real
HS256 JWTs, decoded with the test secret the way main.decode_jwt and
tier_gate's JWT branch decode them. The views are the real blueprint routes.

Owner decision 2026-09-21: Developer exports CSV; GeoJSON stays Pro. /pricing:
"CSV on Developer, every format on Pro".

  /api/v1/transactions/export.csv            Developer and up
  /api/v1/lp/export.csv                      Developer and up (was Pro)
  /api/v1/lp/export.geojson                  Pro and up
  /api/v1/tax-incentives/export?format=csv   Developer and up (be#5214)
  /api/v1/tax-incentives/export?format=json  Pro and up (be#5214)

Measured on origin/main (aa0388ad1; e446ca66e for the tax export) with this
matrix, 2026-09-22. A paying caller got the wall in these shapes:

  Bearer dch_live_<paid>              every wall: tier_gate read X-API-Key and
                                      ?api_key= only, and tried a Bearer that
                                      is not a JWT against api_keys, where MCP
                                      keys do not live
  Developer key, any channel          lp export.csv: walled at Pro
  Founding key (Pro-equivalent)       transactions and lp: each wall typed its
                                      own list of plan names, without it
  free X-API-Key + paid Bearer, or    every wall: only the first key was read
  free X-API-Key + paid ?api_key=
  free login cookie + paid Bearer     every wall: the cookie shadowed the Bearer
  login upgraded after sign-in        every wall: the JWT claim is the plan at
                                      sign-in; api_tier_gating reads the account
  a call from the MCP server          every wall: the key it forwards next to
  (export_dataset reads the lp ones)  its X-Internal-Key resolves FREE in
                                      tier_gate (frontend#1534 keeps that for
                                      caller_is_privileged), while MCP itself
                                      reads a paid key as Pro and admits it

The keyless, free, trial, revoked, unknown and forged rows are the controls:
no wall opens for a caller who has paid for nothing.
"""
import datetime
import pathlib
import re
import sys

import pytest

flask = pytest.importorskip("flask")
jwt = pytest.importorskip("jwt")

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SECRET = "export-walls-test-jwt-secret-of-32-bytes-or-more"  # secretscan:allow (test placeholder)
INTERNAL = "export-walls-test-internal-key"
PUBLIC_ADDR = "203.0.113.9"  # 127.0.0.1 skips the rate limiter and is trusted

DEV = "dch_live_" + "e1" * 16
PRO = "dch_live_" + "e2" * 16
FOUNDING = "dch_live_" + "e3" * 16
FREE = "dch_live_" + "e4" * 16
REVOKED = "dch_live_" + "e5" * 16
UNKNOWN = "dch_live_" + "e6" * 16
TRIAL = "dch_trial_" + "e7" * 16

MCP_KEYS = {  # api_key -> (email, mcp_dev_keys.tier, status); checkout writes 'paid'
    DEV: ("dev@exports.test", "paid", "active"),
    PRO: ("pro@exports.test", "paid", "active"),
    FOUNDING: ("founding@exports.test", "paid", "active"),
    FREE: (None, "free", "active"),
    REVOKED: ("pro@exports.test", "paid", "revoked"),
}
USERS = {  # users.email -> (plan, subscription_status, role, demoted_at)
    "dev@exports.test": ("developer", "active", "", None),
    "pro@exports.test": ("pro", "active", "", None),
    "founding@exports.test": ("founding", "active", "", None),
    "free@exports.test": ("free", "", "", None),
    "webpro@exports.test": ("pro", "active", "", None),
    "upgraded@exports.test": ("developer", "active", "", None),
}


def _login(email, plan, secret=SECRET):
    exp = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
    return jwt.encode({"user_id": email, "email": email, "plan": plan, "exp": exp},
                      secret, algorithm="HS256")


LOGIN_FREE = _login("free@exports.test", "free")
LOGIN_PRO = _login("webpro@exports.test", "pro")
LOGIN_UPGRADED = _login("upgraded@exports.test", "free")  # bought Developer after sign-in
LOGIN_FORGED = _login("pro@exports.test", "pro", secret="not-the-secret-but-as-long-as-one-is")


def _bearer(k):
    return {"Authorization": f"Bearer {k}"}


# name -> (headers, query, login cookie, the plan the caller has paid for)
CALLERS = {
    "keyless": ({}, {}, None, None),
    "free key, X-API-Key": ({"X-API-Key": FREE}, {}, None, "free"),
    "free key, ?api_key=": ({}, {"api_key": FREE}, None, "free"),
    "free key, Bearer": (_bearer(FREE), {}, None, "free"),
    "trial key": ({"X-API-Key": TRIAL}, {}, None, "identified"),
    "revoked paid key, Bearer": (_bearer(REVOKED), {}, None, None),
    "unknown key, Bearer": (_bearer(UNKNOWN), {}, None, None),
    "free login": ({}, {}, LOGIN_FREE, "free"),
    "forged Pro login": ({}, {}, LOGIN_FORGED, None),
    "MCP server, free key": ({"X-Internal-Key": INTERNAL, "X-API-Key": FREE}, {}, None, "free"),
    "Developer key, X-API-Key": ({"X-API-Key": DEV}, {}, None, "developer"),
    "Developer key, ?api_key=": ({}, {"api_key": DEV}, None, "developer"),
    "Developer key, Bearer": (_bearer(DEV), {}, None, "developer"),
    "Pro key, X-API-Key": ({"X-API-Key": PRO}, {}, None, "pro"),
    "Pro key, ?api_key=": ({}, {"api_key": PRO}, None, "pro"),
    "Pro key, Bearer": (_bearer(PRO), {}, None, "pro"),
    "Founding key, X-API-Key": ({"X-API-Key": FOUNDING}, {}, None, "founding"),
    "free login + Pro Bearer key": (_bearer(PRO), {}, LOGIN_FREE, "pro"),
    "free login + Developer X-API-Key": ({"X-API-Key": DEV}, {}, LOGIN_FREE, "developer"),
    "Pro login + free X-API-Key": ({"X-API-Key": FREE}, {}, LOGIN_PRO, "pro"),
    "free X-API-Key + Pro Bearer": ({"X-API-Key": FREE, **_bearer(PRO)}, {}, None, "pro"),
    "free X-API-Key + Developer ?api_key=": ({"X-API-Key": FREE}, {"api_key": DEV}, None, "developer"),
    "login upgraded after sign-in": ({}, {}, LOGIN_UPGRADED, "developer"),
    # MCP reads every paid key as Pro (frontend#1534: MCP keeps its behaviour)
    # and admits it to export_dataset, which calls these routes.
    "MCP server, Developer key": ({"X-Internal-Key": INTERNAL, "X-API-Key": DEV}, {}, None, "pro"),
}

# path -> (the lowest plan the owner sells it on, the wall's status)
WALLS = {
    "/api/v1/transactions/export.csv": ("developer", 402),
    "/api/v1/lp/export.csv": ("developer", 402),
    "/api/v1/lp/export.geojson": ("pro", 402),
    "/api/v1/tax-incentives/export?format=csv": ("developer", 403),
    "/api/v1/tax-incentives/export?format=json": ("pro", 403),
}
_ORDER = {None: -1, "free": 0, "identified": 1, "developer": 2, "pro": 3, "founding": 3}


# ── the fake database ────────────────────────────────────────────────────────

class _Cur:
    def __init__(self):
        self.row = None
        self.rows = []
        self.rowcount = 0
        self.description = None

    def execute(self, sql, params=()):
        s = " ".join(str(sql).split())
        self.row, self.rows = None, []
        if "FROM mcp_dev_keys WHERE api_key = %s" in s and params:
            self.row = MCP_KEYS.get(params[0])
        elif "FROM users WHERE" in s and params:
            self.row = USERS.get(str(params[0]).lower())
        elif "FROM saved_lp_sites WHERE user_id = %s" in s and params:
            # One saved site, named after the account it was read for, so a
            # test can see WHICH credential named the account.
            self.rows = [{
                "id": 1, "name": params[0], "latitude": 39.04, "longitude": -77.49,
                "state": "VA", "market": "Ashburn", "notes": "", "target_mw": 50.0,
                "dcpi_score_at_save": 71,
                "saved_at": datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc),
            }]

    def fetchone(self):
        return self.row

    def fetchall(self):
        return list(self.rows)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    autocommit = True

    def cursor(self, *a, **k):
        return _Cur()

    def close(self):
        pass

    def commit(self):
        pass

    def rollback(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


DEAL = {"id": 7, "date": "2026-01-02", "buyer": "Buyer Co", "seller": "Seller Co",
        "value": 1_250_000_000, "mw": 300, "type": "acquisition", "region": "NA",
        "market": "Ashburn"}


def _decode(token):
    try:
        return jwt.decode(token, SECRET, algorithms=["HS256"])
    except Exception:
        return None


@pytest.fixture(autouse=True)
def world(monkeypatch):
    import psycopg2
    import api_tier_gating as atg
    import routes.lp_sites as lp
    import routes.tier_gate as tg
    import routes.transactions_browser as tb
    from util import mcp_key_plan
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/fake")
    monkeypatch.setenv("NEON_DATABASE_URL", "postgresql://fake/fake")
    monkeypatch.setenv("JWT_SECRET", SECRET)
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", INTERNAL)
    monkeypatch.setattr(psycopg2, "connect", lambda *a, **k: _Conn())
    monkeypatch.setattr(atg, "_decode_jwt_fn", _decode)
    monkeypatch.setattr(lp, "_conn", lambda: _Conn())
    monkeypatch.setattr(tb, "_fetch_deals", lambda **k: ([dict(DEAL)], 1))
    monkeypatch.setattr(tg, "_rl_check", lambda key, per_minute: (True, 0))
    mcp_key_plan._rest_plan_cache.clear()
    yield
    mcp_key_plan._rest_plan_cache.clear()


@pytest.fixture
def client():
    from flask import Flask
    import tax_incentives_routes
    from routes.lp_sites import lp_sites_bp
    from routes.transactions_browser import transactions_browser_bp
    app = Flask(__name__)
    app.register_blueprint(lp_sites_bp)
    app.register_blueprint(transactions_browser_bp)
    tax_incentives_routes.setup_tax_incentive_routes(app)  # no db: DEFAULT_INCENTIVES
    return app.test_client()


def _get(client, path, headers, query, login):
    # The test client's cookie jar writes the Cookie header (a Cookie passed in
    # `headers` is replaced), so a login goes through the jar.
    client.delete_cookie("dchub_token")
    if login:
        client.set_cookie("dchub_token", login)
    path, _, fixed = path.partition("?")
    q = dict(query)
    if fixed:
        q.update(dict(kv.split("=", 1) for kv in fixed.split("&")))
    return client.get(path, headers=dict(headers), query_string=q,
                      environ_base={"REMOTE_ADDR": PUBLIC_ADDR})


def _opens(plan, wall_plan):
    return _ORDER[plan] >= _ORDER[wall_plan]


def _id(text):
    """A test id without spaces, so `FAILED <id>` stays one token."""
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")


@pytest.mark.parametrize("path", sorted(WALLS), ids=_id)
@pytest.mark.parametrize("caller", list(CALLERS), ids=_id)
def test_each_wall_opens_for_the_plan_that_bought_it(client, caller, path):
    headers, query, login, plan = CALLERS[caller]
    wall_plan, wall_status = WALLS[path]
    resp = _get(client, path, headers, query, login)
    if not _opens(plan, wall_plan):
        assert resp.status_code == wall_status, (
            f"{caller} has paid for {plan or 'nothing'} and {path} sells "
            f"{wall_plan}: expected the wall, got {resp.status_code}")
        return
    assert resp.status_code == 200, (
        f"{caller} has paid for {plan}, which opens {path}; got "
        f"{resp.status_code} {resp.get_data(as_text=True)[:200]}")
    if path.startswith("/api/v1/tax-incentives/"):
        if path.endswith("json"):
            assert resp.get_json(force=True)["count"] > 40  # a row per state
        else:
            assert resp.mimetype == "text/csv"
            assert resp.get_data(as_text=True).count("\n") > 40
        return
    if path.endswith(".geojson"):
        assert resp.mimetype == "application/geo+json"
        features = resp.get_json(force=True)["features"]
        assert len(features) == 1
        served_for = features[0]["properties"]["name"]
    else:
        assert resp.mimetype == "text/csv"
        lines = resp.get_data(as_text=True).strip().splitlines()
        assert len(lines) == 2, lines  # the header row and the one row served
        served_for = lines[1].split(",")[1]
    if path.startswith("/api/v1/lp/"):
        # the saved sites read are the account this request names
        assert served_for == _account(headers, query, login)


@pytest.mark.parametrize("caller", [c for c, v in CALLERS.items() if v[3] == "developer"], ids=_id)
def test_the_geojson_wall_tells_a_developer_they_are_on_developer(client, caller):
    """The one wall a Developer still meets names the plan they are on, not FREE."""
    headers, query, login, _ = CALLERS[caller]
    resp = _get(client, "/api/v1/lp/export.geojson", headers, query, login)
    assert resp.status_code == 402
    body = resp.get_json(force=True)
    assert body["current_tier"] == "DEVELOPER", body.get("current_tier")
    assert body["required_tier"] == "PRO"


# ── the resolver itself: every consumer of routes/tier_gate reads it ─────────

def _ctx(headers=None, query=None, login=None):
    from flask import Flask
    h = dict(headers or {})
    if login:
        h["Cookie"] = f"dchub_token={login}"
    return Flask(__name__).test_request_context(
        "/api/v1/deals", headers=h, query_string=query or {},
        environ_base={"REMOTE_ADDR": PUBLIC_ADDR})


@pytest.mark.parametrize("headers,query,login,tier", [
    (_bearer(DEV), {}, None, "DEVELOPER"),
    (_bearer(PRO), {}, None, "PRO"),
    ({"X-API-Key": FREE, **_bearer(PRO)}, {}, None, "PRO"),
    ({"X-API-Key": FREE}, {"api_key": DEV}, None, "DEVELOPER"),
    (_bearer(PRO), {}, LOGIN_FREE, "PRO"),
    (_bearer(FREE), {}, None, "FREE"),
    ({}, {}, None, "FREE"),
], ids=["bearer-dev", "bearer-pro", "free-header+pro-bearer", "free-header+dev-query",
        "free-login+pro-bearer", "bearer-free", "keyless"])
def test_caller_tier_reads_every_key_the_request_presents(headers, query, login, tier):
    """caller_is_privileged and every hard wall read this; the walls also lift."""
    from routes.tier_gate import _resolve_caller_tier
    with _ctx(headers, query, login):
        assert _resolve_caller_tier()[0] == tier


def test_a_wall_that_names_no_known_plan_admits_nobody():
    from routes.tier_gate import caller_meets
    with _ctx(_bearer(PRO)):
        assert caller_meets("PRO") == (True, "PRO")
        assert caller_meets("PR0")[0] is False


def test_the_mcp_servers_own_call_resolves_the_key_with_mcps_mapping():
    """2026-09-25: tier_gate resolves the key the MCP server forwards, with
    MCP's own mapping (paid -> Pro). It read FREE, which locked routes that
    compare the tier name (site_selection_canvas and siblings) for paying
    keys. A direct REST call with the same key still reads the plan it bought."""
    from routes.tier_gate import _resolve_caller_tier
    with _ctx({"X-Internal-Key": INTERNAL, "X-API-Key": DEV}):
        assert _resolve_caller_tier()[0] == "PRO"
    with _ctx({"X-API-Key": DEV}):
        assert _resolve_caller_tier()[0] == "DEVELOPER"


def test_request_api_keys_starts_with_request_api_key():
    """One extraction rule: the list's first key is the single-key reader's key."""
    import api_tier_gating as atg
    assert hasattr(atg, "request_api_keys"), "api_tier_gating.request_api_keys is missing"
    shapes = [
        ({}, {}), ({"X-API-Key": FREE}, {}), ({}, {"api_key": DEV}), (_bearer(PRO), {}),
        ({"X-API-Key": FREE, **_bearer(PRO)}, {"api_key": DEV}),
        ({"X-API-Key": DEV}, {"api_key": DEV}),
        ({"Authorization": "Bearer not-a-key-shape"}, {}),
        ({"Authorization": "Basic " + DEV}, {}),
    ]
    for headers, query in shapes:
        with _ctx(headers, query):
            keys = atg.request_api_keys()
            single = atg.request_api_key()
            assert keys[:1] == ([single] if single else []), (headers, query, keys, single)
            assert len(keys) == len(set(keys)), keys
    with _ctx({"X-API-Key": FREE, **_bearer(PRO)}, {"api_key": DEV}):
        assert atg.request_api_keys() == [FREE, DEV, PRO]


# ── the account an export is read for ────────────────────────────────────────

def _account(headers=None, query=None, login=None):
    from routes.lp_sites import _user_id_from_request
    with _ctx(headers, query, login):
        return _user_id_from_request()


def test_a_bearer_key_names_the_same_account_as_that_key_in_a_header():
    assert _account(_bearer(PRO)) is not None, "a Bearer key named no account (401)"
    assert _account(_bearer(PRO)) == _account({"X-API-Key": PRO})
    assert _account({}, {"api_key": PRO}) == _account({"X-API-Key": PRO})


def test_an_account_already_named_by_another_credential_does_not_move():
    """Sites saved before this change stay where they were saved."""
    assert _account(_bearer(PRO), login=LOGIN_FREE) == _account(login=LOGIN_FREE)
    assert _account({"X-API-Key": FREE, **_bearer(PRO)}) == _account({"X-API-Key": FREE})
    assert _account({"Authorization": "Bearer not-a-key-shape"}) is None
    assert _account() is None
