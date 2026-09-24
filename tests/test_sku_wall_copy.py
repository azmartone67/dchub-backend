"""r-sku-wall (2026-09-24): every wall sells only what is on sale.

The ladder a wall may offer: the $10 one-time credit pack (API capacity only,
never an unlock — owner wording rule 2026-09-22), Developer and Pro at their
tier_registry prices, Land & Power as a Pro product, and Enterprise from
tier_registry.ENTERPRISE_FROM_USD_YEAR. Starter ($9) is retired from every
offer (owner, 2026-09-24) — the tier survives only for grandfathered
subscribers — and Founding, $199, $299, $699 and "$25K" are not prices DC Hub
sells.

The walls below were each measured quoting one of those before this change.
Each test drives the real function where it can be imported; the last one is
a literal scan of the same files, comments stripped, so a retired price cannot
come back in a sentence nobody paying ever reads.
"""
from __future__ import annotations

import ast
import io
import pathlib
import re
import tokenize

import pytest

import tier_registry as tr

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", "sku-wall-copy-test-secret")
    monkeypatch.delenv("DCHUB_GO_LINKS", raising=False)


STARTER_OFFER = re.compile(r"\$9\s*/\s*mo|\$9/month|Starter\s*—\s*\$9|\$9<span")


def _no_starter(text):
    assert not STARTER_OFFER.search(text), text


# ── MCP rate-limit walls ────────────────────────────────────────────────────
@pytest.mark.parametrize("which", ["minute", "day"])
def test_rate_limit_walls_offer_pack_then_developer_not_starter(which):
    import time
    import mcp_gatekeeper as g
    lim, key, cap = g._RateLimiter(), "sku-wall-probe", g.LIMITS[g.Tier.FREE]
    if which == "minute":
        lim._minute[key] = [time.time()] * cap["minute"]
    else:
        lim._day[key] = {lim._today(): cap["day"]}
    wall = lim.check(key, g.Tier.FREE)
    assert wall and wall.startswith("Rate limited"), wall
    _no_starter(wall)
    assert f"{tr.price_display('developer')} Developer" in wall
    assert "$10 one-time" in wall


def test_developer_cta_is_the_rung_this_limiter_lifts():
    import mcp_gatekeeper as g
    assert g.LIMITS[g.Tier.DEVELOPER]["day"] > g.LIMITS[g.Tier.IDENTIFIED]["day"]
    cta = g._developer_cta()
    assert cta.startswith(tr.price_display("developer"))
    assert "https://dchub.cloud/go/c/" in cta
    assert not hasattr(g, "_starter_cta")


# ── /api/v1/upgrade-hint ────────────────────────────────────────────────────
def test_upgrade_hint_prose_names_pack_and_developer_not_starter():
    from flask import Flask
    from routes import mcp_funnel_upgrade as m
    app = Flask("t")
    app.register_blueprint(m.mcp_funnel_upgrade_bp)
    body = app.test_client().get("/api/v1/upgrade-hint").get_json()
    for field in ("agent_quotable", "what_you_get"):
        _no_starter(body[field])
        assert "one-time" in body[field] and "pack" in body[field]
        assert f"${tr.price('developer')}/mo" in body[field]


# ── /pricing/checkout/start downsell ────────────────────────────────────────
def test_checkout_downsell_offers_the_pack_not_starter():
    from routes.checkout_email_capture import _downsell_html
    box = _downsell_html("developer", "rank_sites")
    _no_starter(box)
    assert "tier=metered" in box and "one-time credit pack" in box
    assert "tier=starter" not in box
    assert _downsell_html("metered", "x") == ""


# ── Land & Power: Pro-only (owner 2026-09-22) ──────────────────────────────
@pytest.mark.parametrize("tier", ["free", "identified", "starter", "developer"])
def test_land_power_walls_sell_pro(tier):
    from land_power_usage_limiter import LAND_POWER_LIMITS
    text = LAND_POWER_LIMITS[tier]["upgrade_text"]
    assert "Pro" in text and tr.price_display("pro") in text
    assert "Developer" not in text


# ── upgrade nudge email ────────────────────────────────────────────────────
def test_nudge_email_cheap_card_is_the_pack():
    from routes.upgrade_nudger import _render_nudge_html, _STRIPE_PACK
    html = _render_nudge_html("a@example.com", 3, 180)
    _no_starter(html)
    assert "$10" in html and "one-time" in html
    assert "STARTER" not in html
    assert _STRIPE_PACK in html


# ── near-converter email: sells the plan that opens the tool ───────────────
def _pitch(tool):
    from routes.mcp_usage_self import _draft_near_converter_pitch
    return _draft_near_converter_pitch({
        "paid_403_count": 12, "paid_403_by_tool": {tool: 12},
        "first_seen": "2026-09-01T00:00:00Z", "last_seen": "2026-09-10T00:00:00Z",
    })


@pytest.mark.parametrize("tool", ["analyze_site", "compare_sites",
                                  "get_grid_intelligence", "get_fiber_intel"])
def test_near_converter_pro_tools_quote_pro(tool):
    body = _pitch(tool)
    _no_starter(body)
    assert f"{tr.price_display('pro')} Pro plan unlocks it" in body


def test_near_converter_other_tools_quote_developer_pack_as_capacity_only():
    body = _pitch("get_market_intel")
    _no_starter(body)
    assert f"full depth on the {tr.price_display('developer')} Developer plan" in body
    # the pack is capacity, never an unlock (owner wording rule 2026-09-22)
    assert "unlocks with the $10" not in body
    assert "adds 1,000 API credits" in body


# ── the literal scan ───────────────────────────────────────────────────────
# Offer literals that must not appear in executable code or strings of the
# files this change fixed. Comments are dropped first (they record history).
BANNED = [
    (re.compile(r"\$9\s*/\s*mo|\$9/month"), "Starter $9 offer"),
    (re.compile(r"\$25K", re.I), "Enterprise $25K (registry: from $12,000/yr)"),
    (re.compile(r"\$699/mo"), "retired Team/Enterprise $699/mo"),
    (re.compile(r"Founding cohort"), "Founding offered as a price"),
]
FIXED_FILES = [
    "mcp_gatekeeper.py", "deals_public_api.py", "land_power_usage_limiter.py",
    "routes/agent_concierge.py", "routes/checkout_email_capture.py",
    "routes/mcp_funnel_upgrade.py", "routes/mcp_high_intent_claim.py",
    "routes/mcp_usage_self.py", "routes/upgrade_nudger.py",
    "routes/paywall_hint_middleware.py", "routes/enterprise_leads_sweep.py",
    "routes/email_capture.py", "main.py",
]
# Value-comparison lines quote COMPETITORS' prices (a CBRE seat, "no $25K
# contracts"), which is a claim about their price, not an offer of ours.
ALLOWED = {("mcp_gatekeeper.py", "$25K/yr CBRE seat"),
           ("mcp_gatekeeper.py", "$25K/yr DC Hawk"),
           ("main.py", "$25K contracts"),       # "no $25K contracts, no NDAs"
           ("main.py", "$25K/year for")}        # "What CBRE/JLL charge $25K/year for"


def _allowed(path, code, m):
    tail = code[m.start(): m.end() + 20]
    return any(p == path and tail.startswith(a) for p, a in ALLOWED)


def _code_without_comments(path):
    src = (ROOT / path).read_text(encoding="utf-8")
    toks = tokenize.generate_tokens(io.StringIO(src).readline)
    return tokenize.untokenize(
        (t.type, t.string) for t in toks if t.type != tokenize.COMMENT)


@pytest.mark.parametrize("path", FIXED_FILES)
def test_fixed_walls_carry_no_retired_price_literal(path):
    code = _code_without_comments(path)
    for rx, why in BANNED:
        for m in rx.finditer(code):
            if _allowed(path, code, m):
                continue
            line = code[max(0, m.start() - 40): m.end() + 40]
            raise AssertionError(f"{path}: {why}: …{line}…")


def test_scan_can_fire():
    # a scan that can find nothing proves nothing: the pattern must hit the
    # exact shape it replaced
    assert BANNED[0][0].search('"Starter $9/mo"')
    assert BANNED[1][0].search("from $25K/yr")


def test_stripe_config_plan_info_has_no_monthly_enterprise_price():
    # /api/v2/stripe/config emits PLAN_INFO raw; Enterprise said 699/mo there.
    import api_tier_gating as a
    assert a.PLAN_INFO["enterprise"]["price_monthly"] is None
    assert a.PLAN_INFO["enterprise"]["price_annual"] is None
    # and the gate summary still reads the registry, saying "By contact"
    assert a._build_gate_plans()["enterprise"].startswith("By contact")


# ── expired launch promo (r-claim-promo, 2026-09-24) ─────────────────────
# DCMCP50_LAUNCH ("50% off the first 3 months") expired 2026-07-01; the MCP
# server stopped advertising it that day and stopped attaching it to checkout
# links on 07-12 (Stripe showed "invalid promo code"). The /claim success page
# was never gated and kept promising it to every freshly-claimed trial key.
@pytest.mark.parametrize("render", ["_render_success", "_render_state_success"])
def test_claim_success_page_promises_no_expired_promo(render):
    import routes.mcp_high_intent_claim as m
    args = ("buyer@acme-energy.co", "dch_trial_x")
    html = getattr(m, render)(*args, *(("rank_sites",) if render == "_render_success" else ()))
    assert "dch_trial_x" in html          # it really rendered the success page
    assert "DCMCP50" not in html
    assert "50% off" not in html


def test_no_wall_file_carries_the_expired_promo_code():
    for path in FIXED_FILES:
        assert "DCMCP50" not in _code_without_comments(path), path


# ── r-sku-wall follow-up (2026-09-24): the MCP manifest's pricing table ────────
# GET /api/v1/mcp/manifest published pricing.starter = {$9, stripe_url: the
# Starter Payment Link} after every wall had stopped selling Starter (measured
# live). Two tables in main.py built it: _canonical_pricing() and
# _well_known_tool_gate(). Importing main.py is not cheap, so the rule is read
# off the AST: every "starter" row that carries a stripe_url carries None, and
# says it is retired.
_STARTER_LINK_ID = "8x2dRa5sS0x75uteGuaZi0g"


def _starter_rows_in(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    rows = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for k, v in zip(node.keys, node.values):
            if (isinstance(k, ast.Constant) and k.value == "starter"
                    and isinstance(v, ast.Dict)):
                fields = {fk.value: fv for fk, fv in zip(v.keys, v.values)
                          if isinstance(fk, ast.Constant)}
                if "stripe_url" in fields:
                    rows.append((v.lineno, fields))
    return rows


def test_manifest_pricing_starter_rows_sell_nothing():
    rows = _starter_rows_in(ROOT / "main.py")
    assert len(rows) >= 2, f"expected both manifest tables, found {len(rows)}"
    for line, f in rows:
        url = f["stripe_url"]
        assert isinstance(url, ast.Constant) and url.value is None, \
            f"main.py:{line} starter row carries a buy link"
        st = f.get("status")
        assert isinstance(st, ast.Constant) and st.value == "retired", \
            f"main.py:{line} starter row does not say it is retired"


def test_main_py_carries_no_starter_payment_link():
    assert _STARTER_LINK_ID not in (ROOT / "main.py").read_text(encoding="utf-8")
