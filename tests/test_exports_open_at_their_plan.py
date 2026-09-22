"""The two data exports open for the plans /pricing sells them to
(frontend#1534, 2026-09-22).

/pricing: "PDF reports + exports: CSV on Developer, every format on Pro" and
"M&A transactions: Full + CSV export" from Developer up. Before:

  /api/v1/tax-incentives/export       opened for two literal demo keys and
                                      nothing else, so no customer could export
  /api/v1/transactions/export.csv     gated at DEVELOPER through routes/tier_gate,
                                      which resolved every self-serve MCP key
                                      (dch_live_, dch_oauth_) FREE: a Developer
                                      who bought through MCP got a 402

The tax export now opens CSV at Developer and every other format at Pro. The
transactions export was fixed by #5207 (routes/tier_gate resolves MCP keys);
this pins it.

The real routes run: tax_incentives_routes.setup_tax_incentive_routes (no
database: it serves DEFAULT_INCENTIVES) and the transactions_browser
blueprint. A signed-in user's tier comes from a real signed JWT through the
real resolver; a self-serve key's from validate_api_key, stubbed as a table.
"""
import pathlib
import sys

import flask
import jwt
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

JWT_SECRET = "exports-test-jwt-secret-not-a-secret"
DEV_KEY = "dch_live_" + "d" * 32
PRO_KEY = "dch_live_" + "r" * 32
FREE_KEY = "dch_live_" + "f" * 32
OAUTH_DEV_KEY = "dch_oauth_" + "o" * 32
PLANS = {DEV_KEY: "developer", PRO_KEY: "pro", FREE_KEY: "free", OAUTH_DEV_KEY: "developer"}


@pytest.fixture
def env(monkeypatch):
    import api_tier_gating
    import util.mcp_key_plan as mkp
    monkeypatch.setenv("JWT_SECRET", JWT_SECRET)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    monkeypatch.setattr(api_tier_gating, "validate_api_key",
                        lambda k: {"plan": PLANS[k], "user_id": k} if k in PLANS else None)
    mkp._rest_plan_cache.clear()
    yield
    mkp._rest_plan_cache.clear()


def _jwt(plan):
    return jwt.encode({"plan": plan, "sub": "u-" + plan}, JWT_SECRET, algorithm="HS256")


def _headers(key=None, plan=None):
    h = {"User-Agent": "node"}
    if key:
        h["X-API-Key"] = key
    if plan:
        h["Authorization"] = "Bearer " + _jwt(plan)
    return h


# ── /api/v1/tax-incentives/export ────────────────────────────────────────────

@pytest.fixture
def tax(env):
    import tax_incentives_routes as tr
    app = flask.Flask("tax-export")
    tr.setup_tax_incentive_routes(app)
    c = app.test_client()
    c.environ_base["REMOTE_ADDR"] = "100.64.0.9"
    return c


def _export(c, fmt, **who):
    return c.get(f"/api/v1/tax-incentives/export?format={fmt}", headers=_headers(**who))


@pytest.mark.parametrize("who", [{"plan": "developer"}, {"plan": "pro"}, {"key": DEV_KEY},
                                 {"key": PRO_KEY}, {"key": OAUTH_DEV_KEY}])
def test_csv_opens_at_developer(tax, who):
    r = _export(tax, "csv", **who)
    assert r.status_code == 200, (who, r.get_data(as_text=True)[:200])
    assert r.mimetype == "text/csv"
    assert r.get_data(as_text=True).count("\n") > 40        # a row per state


@pytest.mark.parametrize("who", [{"plan": "pro"}, {"key": PRO_KEY}])
def test_json_opens_at_pro(tax, who):
    r = _export(tax, "json", **who)
    assert r.status_code == 200 and r.get_json()["count"] > 40


@pytest.mark.parametrize("who", [{"plan": "developer"}, {"key": DEV_KEY}])
def test_json_below_pro_names_pro(tax, who):
    r = _export(tax, "json", **who)
    body = r.get_json()
    assert r.status_code == 403
    assert body["required_plan"] == "pro" and body["pro_required"] is True


@pytest.mark.parametrize("who", [{}, {"plan": "free"}, {"key": FREE_KEY},
                                 {"key": "dch_live_" + "u" * 32}])
def test_csv_below_developer_names_developer(tax, who):
    r = _export(tax, "csv", **who)
    body = r.get_json()
    assert r.status_code == 403
    assert body["required_plan"] == "developer" and body["pro_required"] is False
    opts = {o["plan"]: o["opens"] for o in body.get("upgrade_options") or []}
    assert opts.get("developer") == "rest"


@pytest.mark.parametrize("key", ["dchub-pro-demo", "dchub-enterprise-demo"])
@pytest.mark.parametrize("fmt", ["csv", "json"])
def test_the_demo_keys_keep_working(tax, key, fmt):
    assert _export(tax, fmt, key=key).status_code == 200


# ── /api/v1/transactions/export.csv ─────────────────────────────────────────

ROWS = [{"id": i, "date": "2026-09-0%d" % i, "buyer": "Buyer %d" % i, "seller": "Seller %d" % i,
         "value": 100 * i, "mw": 10 * i, "type": "ma", "region": "NA", "market": "Dallas"}
        for i in range(1, 6)]


@pytest.fixture
def tx(env, monkeypatch):
    import routes.transactions_browser as tb
    monkeypatch.setattr(tb, "_fetch_deals", lambda **kw: (ROWS[:kw.get("limit", 5)], len(ROWS)))
    app = flask.Flask("tx-export")
    app.register_blueprint(tb.transactions_browser_bp)
    c = app.test_client()
    c.environ_base["REMOTE_ADDR"] = "100.64.0.9"
    return c


@pytest.mark.parametrize("who", [{"key": DEV_KEY}, {"key": OAUTH_DEV_KEY}, {"key": PRO_KEY},
                                 {"plan": "developer"}])
def test_transactions_csv_opens_for_a_self_serve_developer(tx, who):
    r = tx.get("/api/v1/transactions/export.csv", headers=_headers(**who))
    assert r.status_code == 200, (who, r.status_code)
    lines = r.get_data(as_text=True).strip().splitlines()
    assert lines[0].startswith("id,date,buyer,seller,value,mw") and len(lines) == 1 + len(ROWS)


@pytest.mark.parametrize("who", [{}, {"key": FREE_KEY}, {"plan": "free"}])
def test_transactions_csv_below_developer_is_refused(tx, who):
    r = tx.get("/api/v1/transactions/export.csv", headers=_headers(**who))
    assert r.status_code in (402, 403), r.status_code
    assert "id,date,buyer" not in r.get_data(as_text=True)
