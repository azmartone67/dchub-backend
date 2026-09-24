"""r-checkout-metered (2026-09-24): /checkout/start names the plan it charges.

Measured on origin/main before this change:
  * /checkout/start?tier=metered rendered "Metered · $49/mo · cancel anytime"
    (the page's price map had no 'metered', so it fell back to Developer's), and
  * POST /checkout/initiate {tier:'metered'} sent the buyer to the DEVELOPER
    Payment Link (the local link map had no 'metered' either).
Label and charge agreed — both wrong for someone who asked for the $10 pack.
Every tier also saw the same hardcoded "1,000 MCP calls/day across all 24
tools". The page and the link now share one tier resolver (_checkout_tier)
and one canon-built plan table.
"""
from __future__ import annotations

import json
import re

import pytest

import tier_registry as tr


@pytest.fixture()
def ec(monkeypatch):
    import routes.email_capture as mod
    monkeypatch.setattr(mod, "_record_capture", lambda *a, **k: None)
    return mod


@pytest.fixture()
def client(ec):
    from flask import Flask
    app = Flask("t")
    app.register_blueprint(ec.email_capture_bp)
    return app.test_client()


def _plans(client):
    html = client.get("/checkout/start?tier=metered").get_data(as_text=True)
    m = re.search(r"var PLANS = (\{.*?\});\n", html)
    assert m, "the page carries no plan table"
    return html, json.loads(m.group(1))


@pytest.mark.parametrize("asked,charged", [
    ("metered", "metered"), ("pack5", "metered"),
    ("developer", "developer"), ("pro", "pro"), ("founding", "pro"),
    ("starter", "developer"),      # retired from every offer (owner 2026-09-24)
    ("nonsense", "developer"),
])
def test_initiate_charges_the_plan_the_page_names(client, asked, charged):
    from routes._stripe_links import STRIPE_LINKS
    r = client.post("/checkout/initiate",
                    json={"email": "buyer@acme-energy.co", "tier": asked}).get_json()
    assert r["ok"] and r["tier"] == charged
    assert r["checkout_url"].split("?")[0] == STRIPE_LINKS[charged]
    _, P = _plans(client)
    shown = P["aliases"].get(asked, asked)
    shown = shown if shown in P["plans"] else "developer"
    assert shown == charged, "page and link resolve the tier differently"


def test_the_pack_page_sells_the_pack_as_capacity(client):
    _, P = _plans(client)
    pack = P["plans"]["metered"]
    assert pack["label"] == "Credit pack"
    assert pack["price"].startswith("$10 one-time")
    assert "cancel anytime" not in pack["price"]
    text = " ".join(pack["points"]).lower()
    # owner wording rule 2026-09-22: API capacity only, never an unlock
    assert "full depth" not in text and "unlock" not in text


def test_plan_prices_and_calls_come_from_the_registry(client):
    html, P = _plans(client)
    assert P["plans"]["developer"]["price"].startswith(tr.price_display("developer"))
    assert P["plans"]["pro"]["price"].startswith(tr.price_display("pro"))
    assert f"{tr.calls_per_day('developer'):,} MCP calls/day" in P["plans"]["developer"]["points"]
    assert "across all 24 tools" not in html
    assert "__PLANS_JSON__" not in html
