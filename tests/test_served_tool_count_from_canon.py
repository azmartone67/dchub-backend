"""Agent-facing copy states the MCP tool count from the canon, never a literal.

Audit 2026-09-26: live surfaces served four different stale counts while
tools/list served 92 — the gated-call coaching `learn.hint` ("73 tools"),
/openapi-live.json's /mcp summary ("24 tools"), /api/v1/onboard?type=mcp
("48 tools") and /api/v1/mcp/quality ("53 tools"). Each now renders
{canon_tools}. No network, no DB: the probes and count lookups are stubbed.
"""
import re

from flask import Flask

from ai_surface_canon import canon_nums

_COUNT = re.compile(r"(?<![\d,])(\d{1,3})\s+(?:MCP\s+)?tools\b")


def _want():
    n = canon_nums()["{canon_tools}"]
    assert n and n.isdigit() and int(n) >= 90, n
    return n


def _assert_canon(text, where):
    assert "{canon_" not in text, (where, text)
    counts = _COUNT.findall(text)
    assert counts == [_want()], (where, counts, text)


def test_coaching_learn_hint():
    from routes.email_capture import build_agent_coaching
    hint = build_agent_coaching("get_grid_intelligence", "GET /x")["learn"]["hint"]
    _assert_canon(hint, "build_agent_coaching.learn.hint")


def test_onboard_mcp_method():
    import routes.onboard_universal as ou
    app = Flask(__name__)
    app.register_blueprint(ou.onboard_universal_bp)
    d = app.test_client().get("/api/v1/onboard?type=mcp").get_json()
    _assert_canon(d["method"], "/api/v1/onboard?type=mcp")


def test_openapi_live_mcp_summary(monkeypatch):
    import routes.openapi_dynamic as od
    monkeypatch.setattr(od, "_get_counts", lambda: {"facilities": 20000, "deals": 1900,
                                                   "as_of": "2026-09-26T00:00:00Z"})
    app = Flask(__name__)
    app.register_blueprint(od.openapi_dynamic_bp)
    d = app.test_client().get("/openapi-live.json").get_json()
    _assert_canon(d["paths"]["/mcp"]["post"]["summary"], "/openapi-live.json /mcp")


def test_quality_badge_capabilities(monkeypatch):
    import routes.mcp_quality_badge as qb
    monkeypatch.setattr(qb, "_get", lambda *a, **k: None)
    detail = qb.compute_quality()["components"]["capabilities"]["detail"]
    _assert_canon(detail, "/api/v1/mcp/quality capabilities")
