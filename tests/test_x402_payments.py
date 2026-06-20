"""Guards for the x402 agent-autonomous payment rail (2026-06-20).

Locks the safety + pricing contract: ships DARK, advertises price without a
wallet, refuses to verify until armed, never custodies funds, prices the
flagship value-moment tools correctly.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

x = pytest.importorskip("routes.x402_payments")


def _clear_env(monkeypatch):
    for k in ("X402_ENABLED", "X402_RECIPIENT_ADDRESS", "X402_PRICE_OVERRIDES",
              "X402_NETWORK", "X402_USDC_ASSET", "X402_FACILITATOR_URL"):
        monkeypatch.delenv(k, raising=False)


# ── pricing ──────────────────────────────────────────────────────────
def test_flagship_price_is_ten_cents(monkeypatch):
    _clear_env(monkeypatch)
    assert x.price_for("get_grid_intelligence") == 0.10
    assert x.price_for("get_fiber_intel") == 0.10


def test_deep_and_standard_prices(monkeypatch):
    _clear_env(monkeypatch)
    assert x.price_for("analyze_site") == 0.50
    assert x.price_for("get_market_intel") == 0.03  # default/standard


def test_price_override_env_wins(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("X402_PRICE_OVERRIDES", '{"get_grid_intelligence": 0.25}')
    assert x.price_for("get_grid_intelligence") == 0.25
    # one-field tune: a bad JSON override degrades to the table, never raises
    monkeypatch.setenv("X402_PRICE_OVERRIDES", "not json")
    assert x.price_for("get_grid_intelligence") == 0.10


def test_atomic_units_usdc_6_decimals():
    assert x._atomic(0.10) == "100000"   # $0.10 -> 100000 atomic USDC
    assert x._atomic(0.50) == "500000"
    assert x._atomic(0.03) == "30000"


# ── dark-by-default + wallet safety ──────────────────────────────────
def test_ships_dark(monkeypatch):
    _clear_env(monkeypatch)
    assert x.x402_enabled() is False


def test_requirements_marks_unconfigured_without_wallet(monkeypatch):
    _clear_env(monkeypatch)
    req = x.payment_requirements("get_grid_intelligence")
    assert req["configured"] is False           # no wallet -> not receivable
    assert req["accepts"][0]["payTo"] == ""      # no address baked in
    assert req["accepts"][0]["maxAmountRequired"] == "100000"
    assert req["price_usd"] == 0.10


def test_requirements_configured_with_wallet(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("X402_RECIPIENT_ADDRESS", "0xabcDEF0000000000000000000000000000000001")
    req = x.payment_requirements("get_fiber_intel")
    assert req["configured"] is True
    assert req["accepts"][0]["payTo"].startswith("0xabc")


# ── endpoints ────────────────────────────────────────────────────────
@pytest.fixture()
def client(monkeypatch):
    flask = pytest.importorskip("flask")
    _clear_env(monkeypatch)
    app = flask.Flask(__name__)
    x.register_x402_payments(app)
    return app.test_client()


def test_quote_advertises_price_without_wallet(client):
    r = client.get("/api/v1/x402/quote?tool=get_grid_intelligence")
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] and j["tool"] == "get_grid_intelligence"
    assert j["payment"]["price_usd"] == 0.10
    # dark + no wallet -> NOT machine_payable yet, but price is advertised
    assert j["machine_payable"] is False


def test_quote_requires_tool(client):
    assert client.get("/api/v1/x402/quote").status_code == 400


def test_verify_refuses_when_dark(client):
    r = client.post("/api/v1/x402/verify?tool=get_grid_intelligence",
                    headers={"X-PAYMENT": "deadbeef"})
    assert r.status_code == 503
    assert r.get_json()["error"] == "x402_disabled"


def test_verify_not_configured_when_armed_without_wallet(client, monkeypatch):
    monkeypatch.setenv("X402_ENABLED", "true")  # armed but NO wallet
    r = client.post("/api/v1/x402/verify?tool=get_grid_intelligence",
                    headers={"X-PAYMENT": "deadbeef"})
    assert r.status_code == 503
    assert r.get_json()["error"] == "not_configured"


def test_verify_unverified_payment_returns_402(client, monkeypatch):
    monkeypatch.setenv("X402_ENABLED", "true")
    monkeypatch.setenv("X402_RECIPIENT_ADDRESS", "0xabc0000000000000000000000000000000000001")
    # facilitator can't verify a junk payment -> 402, never unlocks
    monkeypatch.setattr(x, "_facilitator_verify", lambda p, r: (False, {"reason": "bad"}))
    r = client.post("/api/v1/x402/verify?tool=get_grid_intelligence",
                    headers={"X-PAYMENT": "junk"})
    assert r.status_code == 402
    assert r.get_json()["ok"] is False


def test_verify_mints_unlock_on_valid_payment(client, monkeypatch):
    monkeypatch.setenv("X402_ENABLED", "true")
    monkeypatch.setenv("X402_RECIPIENT_ADDRESS", "0xabc0000000000000000000000000000000000001")
    monkeypatch.setattr(x, "_facilitator_verify", lambda p, r: (True, {"payment": "settled"}))
    monkeypatch.setattr(x, "_record_unlock", lambda *a, **k: None)  # no DB in test
    r = client.post("/api/v1/x402/verify?tool=get_grid_intelligence",
                    headers={"X-PAYMENT": "validproof"})
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] and j["tool"] == "get_grid_intelligence"
    assert j["unlock_token"].startswith("x402_")


def test_register_wires_routes_onto_real_app():
    flask = pytest.importorskip("flask")
    app = flask.Flask(__name__)
    x.register_x402_payments(app)
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert "/api/v1/x402/quote" in rules
    assert "/api/v1/x402/verify" in rules
