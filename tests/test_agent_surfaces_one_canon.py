"""Agent surfaces bind to the ONE canon (QA-sweep 2026-09-02: D7, F7, pricing 3/6).

What was measured before this change (all live, 2026-09-02 ~00:30Z):
  - Railway origin /.well-known/mcp.json: "18,500+ facilities" x3 and
    "1,400+ tracked deals" beside its own request-time "20,100+" — the tool
    catalog ran canon_text() at IMPORT and froze the cold-start pin; "369 GW"
    (retired 07-27) x2.
  - /llms.txt: a hand-typed "83 tools" plus a retired-count parenthetical
    ("53 tools") that partner-sync flagged daily; no deals floor; "Pro API
    (Key Required — $49/mo)" — neither Pro's price nor the plan that sells.
  - /openapi.json: version 2.12.1 from PINNED while the manifest served the
    live-resolved 2.12.3.
  - /api/v1/upgrade-hint: free=1,000/day, starter=10,000/day, "$199/mo Pro";
    /api/v1/ai-agents.json when_blocked free_tier 10,000/day; mcp_upgrade_gate
    default 100; 429 record-cap copy "Pro ($199/mo)".
  - /api/v1/paywall/checkout with no tier -> developer $49; founding $99
    (the only SKU with completed web-direct checkouts) was invisible.

Every guard here was mutation-verified — see the PR body for the record.
CI-SAFETY: flask-dependent tests importorskip; source-level checks run bare.
"""
import ast
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _served_lines(rel):
    """Source lines that are not pure comments."""
    return [ln for ln in _read(rel).splitlines() if not ln.lstrip().startswith("#")]


def _func_source(rel, name):
    tree = ast.parse(_read(rel))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(_read(rel), node), node
    raise AssertionError(f"{rel}: no function {name}")


# ── D7 · the tool catalog resolves canon PER CALL ─────────────────────────

@pytest.fixture(scope="module")
def catalog():
    pytest.importorskip("flask")
    from routes import mcp_tool_catalog as m
    return m


def test_catalog_descriptions_carry_the_live_floor_not_the_import_time_pin(catalog, monkeypatch):
    """The 18,500+ x3 at the origin: a live floor measured AFTER import must
    reach the served descriptions. Fails if the catalog is a module-level
    list again (canon_text() run once, at import)."""
    import ai_surface_canon as c
    monkeypatch.setattr(c, "_live_public_floors",
                        lambda: {"facilities": "99,900+", "deals": "9,900+"})
    monkeypatch.setattr(catalog, "_live_tools_map", lambda: {})
    wk = {t["name"]: t["description"] for t in catalog.tools_for_well_known()}
    assert "99,900+" in wk["search_facilities"], wk["search_facilities"]
    assert "9,900+" in wk["list_transactions"], wk["list_transactions"]
    assert "18,500+" not in wk["search_facilities"]
    card = {t["name"]: t["description"] for t in catalog.flat_tools_for_card()}
    assert "99,900+" in card["search_facilities"]


def test_catalog_serves_no_retired_pipeline_or_deal_literals(catalog, monkeypatch):
    monkeypatch.setattr(catalog, "_live_tools_map", lambda: {})
    blob = " ".join(t["description"] for t in catalog.tools_for_well_known())
    for bad in ("369 GW", "1,400+", "540+ projects"):
        assert bad not in blob, bad


_RETIRED_SERVED = ("369 GW", "1,400+ tracked", "18,500+ facilities", "28+ tools")
_MANIFEST_PRODUCERS = ("main.py", "routes/mcp_tool_catalog.py",
                       "routes/openapi_autogen.py", "ai_discovery_routes.py")


def _docstring_positions(tree):
    """Positions of every BARE string statement — docstrings and the
    documentation blocks some modules keep after their imports. A bare
    string expression is a no-op at runtime; nothing serves it."""
    pos = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        for stmt in body:
            if (isinstance(stmt, ast.Expr)
                    and isinstance(getattr(stmt, "value", None), ast.Constant)
                    and isinstance(stmt.value.value, str)):
                pos.add((stmt.value.lineno, stmt.value.col_offset))
    return pos


def _string_literals(rel):
    """(lineno, value) of every non-docstring string literal in `rel`."""
    src = _read(rel)
    tree = ast.parse(src)
    docs = _docstring_positions(tree)
    return [(n.lineno, n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and (n.lineno, n.col_offset) not in docs]


@pytest.mark.parametrize("rel", _MANIFEST_PRODUCERS)
def test_manifest_producers_carry_no_retired_literals(rel):
    """Retired LITERALS stay wrong forever (a range grows into truth — see
    surface_truth_master_shell). String literals only, docstrings excluded:
    the history recorded in docstrings and comments is the point of keeping it."""
    hits = [(ln, v.strip()[:100]) for ln, v in _string_literals(rel)
            if any(b in v for b in _RETIRED_SERVED)]
    assert not hits, f"{rel} still serves retired literal(s): {hits}"


# ── F7 · openapi.json version = the served resolver, not the pin ──────────

def test_openapi_autogen_version_is_the_served_resolver(monkeypatch):
    pytest.importorskip("flask")
    from flask import Flask
    import ai_surface_canon as c
    from routes import openapi_autogen as oa
    monkeypatch.setattr(c, "resolve_server_version_cached", lambda: "9.9.9")
    spec = oa._build_spec(Flask("t"))
    assert spec["info"]["version"] == "9.9.9"


def test_root_openapi_and_llms_txt_read_the_served_resolver_and_canon():
    """ai_discovery_routes registers inside a factory (importing it boots the
    app), so the contract is pinned at source level: the openapi producer
    names the served resolver, and llms.txt has no hand-typed tool count,
    no retired-count parenthetical, and no hand-typed monthly price."""
    src, _ = _func_source("ai_discovery_routes.py", "serve_openapi_json")
    assert "resolve_server_version_cached" in src
    src, node = _func_source("ai_discovery_routes.py", "serve_llms_txt")
    assert "_llms_paid_heading()" in src
    assert "{canon_tools} tools" in src
    assert "{canon_deals}" in src
    lits = [n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    blob = "\n".join(lits)
    assert not re.search(r"\b\d{2,3} tools\b", blob), "hand-typed tool count in llms.txt"
    assert "53 tools" not in blob
    assert not re.search(r"\$\d+/mo", blob), "hand-typed monthly price in llms.txt"


# ── pricing 3 · llms.txt paid heading from the pricing canon ──────────────

@pytest.fixture(scope="module")
def discovery():
    pytest.importorskip("flask")
    import ai_discovery_routes as m
    return m


def _reset_heading(discovery):
    discovery._paid_heading_cache.update(at=0.0, val=None)


def test_llms_paid_heading_shows_founding_only_while_open(discovery, monkeypatch):
    import routes.founding_customers as fc
    import tier_registry as tr
    monkeypatch.setattr(tr, "price", lambda t: {"developer": 49, "founding": 99}.get(t, 0))
    monkeypatch.setattr(fc, "founding_status", lambda: {"program_active": True})
    _reset_heading(discovery)
    h = discovery._llms_paid_heading()
    assert "Founding Member $99/mo" in h and "Developer $49/mo" in h, h
    monkeypatch.setattr(fc, "founding_status", lambda: {"program_active": False})
    _reset_heading(discovery)
    h = discovery._llms_paid_heading()
    assert "Founding" not in h and "Developer $49/mo" in h, h


def test_llms_paid_heading_tracks_the_registry_price(discovery, monkeypatch):
    import routes.founding_customers as fc
    import tier_registry as tr
    monkeypatch.setattr(fc, "founding_status", lambda: {"program_active": False})
    monkeypatch.setattr(tr, "price", lambda t: 77 if t == "developer" else 0)
    _reset_heading(discovery)
    assert "Developer $77/mo" in discovery._llms_paid_heading()


# ── D7 · surface-truth audits the PRE-EDGE origin ─────────────────────────

@pytest.fixture(scope="module")
def shell():
    pytest.importorskip("flask")
    from routes import surface_truth_master_shell as m
    return m


def test_surface_truth_pre_edge_lane_fetches_the_origin_host(shell, monkeypatch):
    seen = []

    def fake_fetch(path, base=None):
        seen.append((path, base))
        return "18,500+ facilities", None

    monkeypatch.setattr(shell, "_fetch", fake_fetch)
    checks = shell._lane_pre_edge_origin("18,500+")
    assert {b for _, b in seen} == {shell.PRE_EDGE_ORIGIN}
    assert "railway.app" in shell.PRE_EDGE_ORIGIN
    assert {p for p, _ in seen} >= {"/.well-known/mcp.json", "/llms.txt", "/openapi.json"}
    assert checks and all(k["pass"] for k in checks)


def test_surface_truth_edge_clean_origin_stale_is_visible(shell, monkeypatch):
    """The 2026-09-02 shape: edge serves canon, origin serves a retired floor."""
    def fake_fetch(path, base=None):
        # valid JSON either way — the manifest lane also runs a (non-critical)
        # "parses" check, and any False check fails a lane.
        return ('{"d": "12,650+ facilities"}' if base == shell.PRE_EDGE_ORIGIN
                else '{"d": "18,500+ facilities"}'), None

    monkeypatch.setattr(shell, "_fetch", fake_fetch)
    monkeypatch.setattr(shell, "_read_repo", lambda rel: '{"d": "18,500+ facilities"}')
    monkeypatch.setattr(shell, "_beat_ledger", lambda *a, **k: None)
    out = shell._run_tick()
    by = {ln["id"]: ln["verdict"] for ln in out["lanes"]}
    assert by["served_manifests"] == "PASS"
    assert by["pre_edge_origin"] == "FAIL", by
    assert out["any_fail"] is True


# ── pricing 6 · ONE guard: served-text producers carry no hand-typed caps/prices

_PRICE_PRODUCERS = ("routes/mcp_funnel_upgrade.py", "routes/stripe_direct_upgrade.py",
                    "api_tier_gating.py", "mcp_upgrade_gate.py")
_CAP_OR_PRICE = [re.compile(p) for p in (
    r"\$\d[\d,]*\s*/\s*mo(?:nth)?\b",                         # "$49/mo", "$199/month"
    r"\b\d[\d,]*\s*(?:calls|records|requests|queries)\s*/\s*day\b",
    r"\b\d[\d,]*/day\b",                                      # "200/day"
    r"\b\d[\d,]* per day\b",
)]


@pytest.mark.parametrize("rel", _PRICE_PRODUCERS)
def test_price_producers_have_no_underived_cap_or_price_literals(rel):
    """String LITERALS (not f-string interpolations, not docstrings) that
    state a cap or a price. Every such number must be a tier_registry read —
    the literal dicts this replaces served five different free caps at once."""
    src = _read(rel)
    tree = ast.parse(src)
    docs = _docstring_positions(tree)
    hits = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if (node.lineno, node.col_offset) in docs:
            continue
        for pat in _CAP_OR_PRICE:
            m = pat.search(node.value)
            if m:
                hits.append((node.lineno, m.group(0), node.value[:70]))
                break
    assert not hits, f"{rel}: hand-typed cap/price literal(s): {hits}"


def test_ai_agents_when_blocked_free_cap_is_canon_derived():
    src = _read("main.py")
    m = re.search(r'"free_tier":\s*\{"calls_per_day":\s*([^,]+),', src)
    assert m, "when_blocked free_tier block missing"
    assert m.group(1).startswith("_canon_int("), m.group(1)


def test_upgrade_hint_tiers_are_registry_reads():
    """★2026-09-02 merge: this called the private _hint_tiers() helper. The
    helper is gone — the table is a dict literal inside the handler again, so
    scripts/api_response_contract.py can still see `tiers.<tier>.<field>` (a
    Call value marks the level OPEN and read as 30 KEY REMOVED breaks). Assert
    against the SERVED response instead, which is the stronger claim anyway:
    it is what an agent quotes."""
    pytest.importorskip("flask")
    from flask import Flask
    import tier_registry as tr
    from routes import mcp_funnel_upgrade as m
    app = Flask("t")
    app.register_blueprint(m.mcp_funnel_upgrade_bp)
    r = app.test_client().get("/api/v1/upgrade-hint")
    assert r.status_code == 200, r.data
    t = r.get_json()["tiers"]
    for tier in ("anonymous", "free", "starter", "developer", "pro"):
        assert t[tier]["calls_per_day"] == tr.calls_per_day(tier), tier
    for tier in ("starter", "developer", "pro"):
        assert t[tier]["price_usd_month"] == tr.price(tier), tier
        assert f"${tr.price(tier)}/mo" in t[tier]["label"]
        assert f"{tr.calls_per_day(tier):,}/day" in t[tier]["label"]
    assert (t["anonymous"]["calls_per_day"] <= t["free"]["calls_per_day"]
            < t["starter"]["calls_per_day"] < t["developer"]["calls_per_day"]
            < t["pro"]["calls_per_day"]), "caps must ascend with price"


def test_mcp_upgrade_gate_free_default_is_the_registry(monkeypatch):
    pytest.importorskip("psycopg")
    import importlib
    import tier_registry as tr
    monkeypatch.delenv("MCP_FREE_DAILY_LIMIT", raising=False)
    import mcp_upgrade_gate as g
    g = importlib.reload(g)
    assert g.FREE_DAILY_LIMIT == tr.calls_per_day("free")
    monkeypatch.setenv("MCP_FREE_DAILY_LIMIT", "37")
    assert importlib.reload(g).FREE_DAILY_LIMIT == 37
    monkeypatch.delenv("MCP_FREE_DAILY_LIMIT", raising=False)
    importlib.reload(g)


def test_record_cap_429_copy_reads_registry_prices_and_caps():
    pytest.importorskip("flask")
    from flask import Flask
    import api_tier_gating as a
    import tier_registry as tr
    with Flask("t").app_context():
        resp, status, _hdrs = a.build_record_cap_error("k", "developer", 500, 500)
        body = resp.get_json()
    assert status == 429
    msg = body["upgrade"]["message"]
    assert f"${tr.price('pro')}/mo" in msg, msg
    assert f"{tr.TIER_LIMITS['pro']['record_cap']:,} records/day" in msg, msg
    assert "$199" not in msg
    with Flask("t").app_context():
        resp, _s, _h = a.build_record_cap_error("k", "free", 50, 50)
        msg = resp.get_json()["upgrade"]["message"]
    assert f"${tr.price('developer')}/mo" in msg
    assert f"{tr.TIER_LIMITS['developer']['record_cap']:,} records/day" in msg


# ── pricing 3 · /api/v1/paywall/checkout defaults to founding while open ─

@pytest.fixture(scope="module")
def checkout_app():
    pytest.importorskip("flask")
    from flask import Flask
    from routes import stripe_direct_upgrade as sd
    app = Flask("t")
    app.register_blueprint(sd.stripe_direct_bp)
    return app, sd


def _get(app, qs):
    r = app.test_client().get("/api/v1/paywall/checkout" + qs)
    assert r.status_code == 200, r.data
    return r.get_json()


# ★2026-09-02 MERGE NOTE. These were written against
# ?tool=get_dchub_recommendation, a tool that is NOT in TOOL_TIER_MAP — so it
# exercised resolve_tier's fall-through, which was "developer" at the time.
# main has since moved that fall-through to PACK_TIER (the $10 one-time pack):
# a caller we cannot classify gets the cheapest non-recurring offer, not a
# $49/mo plan. That rule is deliberately NOT touched here, and
# test_an_unclassified_caller_still_gets_the_pack below pins it. The founding
# lift is for the case it was actually written for: a TOOL_TIER_MAP row that
# NAMES "developer", with no explicit ?tier=.
_DEV_TOOL = "search_facilities"     # TOOL_TIER_MAP -> developer


def test_the_dev_tool_fixture_really_is_a_developer_row():
    """If this stops being true the two tests below prove nothing."""
    from routes._stripe_links import TOOL_TIER_MAP
    assert TOOL_TIER_MAP.get(_DEV_TOOL) == "developer", TOOL_TIER_MAP.get(_DEV_TOOL)


def test_checkout_defaults_to_founding_while_the_program_is_open(checkout_app, monkeypatch):
    app, sd = checkout_app
    import routes.founding_customers as fc
    monkeypatch.setattr(fc, "founding_status", lambda: {"program_active": True})
    j = _get(app, f"?tool={_DEV_TOOL}")
    assert j["tier"] == "founding", j
    assert j["checkout_url"].startswith(sd.STRIPE_LINKS["founding"])
    assert j["tier_pricing"] == "$99/mo"


def test_checkout_falls_back_to_developer_when_seats_are_gone(checkout_app, monkeypatch):
    app, sd = checkout_app
    import routes.founding_customers as fc
    monkeypatch.setattr(fc, "founding_status", lambda: {"program_active": False})
    j = _get(app, f"?tool={_DEV_TOOL}")
    assert j["tier"] == "developer", j
    assert j["checkout_url"].startswith(sd.STRIPE_LINKS["developer"])
    assert j["tier_pricing"] == "$49/mo"


def test_an_unclassified_caller_still_gets_the_pack(checkout_app, monkeypatch):
    """The founding lift must not resurrect the $49/mo default for a caller
    nobody classified — that is the leak main fixed (102 relay opens, 0 paid)."""
    from routes._stripe_links import PACK_TIER, TIER_PRICE_LABEL
    app, sd = checkout_app
    import routes.founding_customers as fc
    monkeypatch.setattr(fc, "founding_status", lambda: {"program_active": True})
    for qs in ("", "?tool=not_a_real_tool"):
        j = _get(app, qs)
        assert j["tier"] == PACK_TIER, (qs, j)
        assert j["tier_pricing"] == TIER_PRICE_LABEL[PACK_TIER], (qs, j)


def test_checkout_never_overrides_an_explicit_or_pro_choice(checkout_app, monkeypatch):
    app, sd = checkout_app
    import routes.founding_customers as fc
    monkeypatch.setattr(fc, "founding_status", lambda: {"program_active": True})
    assert _get(app, "?tier=developer")["tier"] == "developer"
    j = _get(app, "?tool=analyze_site")          # TOOL_TIER_MAP -> pro
    assert j["tier"] == "pro" and j["tier_pricing"] == "$299/mo", j
    assert _get(app, "?tier=enterprise")["tier_pricing"] == "Custom"
