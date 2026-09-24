"""PRESENT IS NOT VERIFIED: /api/v1/brain/mcp-registries grades each listing's COPY.

`verdict == "present"` only says our marker is on the page. /ai counted every
present listing as "verified live", so mcp.so — serving "79 tools, 12,650+
facilities" and "Pro: $299/mo" against canon 92 / 24,500+ / $99 (measured
2026-09-24) — was counted exactly like Smithery's current copy.

These pin the copy check that now rides on every present result as `copy`,
and the endpoint's `present_stale_copy` list the page reads. Fixtures are the
live bytes measured 2026-09-24. No network: _fetch and canon are stubbed.
"""
from __future__ import annotations

import pytest

from routes import mcp_registry_watch as w

CANON = {"ok": True, "tools": 92, "facilities": "24,500+",
         "fiber_routes": "58,000+", "substations": "133,000+",
         "transmission_lines": "94,000+", "deals": "1,600+"}
PRO = 99

# https://mcp.so/servers/dchub-mcp-server, 2026-09-24 (meta description + FAQ).
MCPSO = ('<meta name="description" content="Live data-center, grid, fiber &amp; '
         'M&amp;A intelligence for AI agents — 79 tools, 12,650+ facilities."/>'
         '<p>Anonymous: 10 calls/day (no key). Free key: 50 calls/day. Starter: '
         '$9/mo for 200 calls/day. Developer: $49/mo for 500 calls/day. Pro: '
         '$299/mo for 2,000 calls/day.</p>')
# https://smithery.ai/servers/azmartone67/dchub, 2026-09-24 — our current copy,
# lagging canon only on one floor (127k vs 133k substations).
SMITHERY = ("DC Hub is the neutral, real-time data layer for electricity. "
            "Coverage: 24,500+ data centers across 170+ countries. 127k substations. "
            "ADVISORY router: collapses 92 tools to one starting point. "
            "Pro: $99/mo.")
# https://glama.ai/mcp/servers/azmartone67/dchub-mcp-server meta, 2026-09-24.
GLAMA = ("DC Hub is the live Model Context Protocol layer for data-center and "
         "energy infrastructure. AI agents call 91 tools across 22,900+ facilities")
# punkpeye/awesome-mcp-servers README: OUR line, plus a foreign neighbour line
# that carries figures of its own.
AWESOME_OURS = ("- [azmartone67/dchub-mcp-server](https://github.com/azmartone67/"
                "dchub-mcp-server) - Data-center, power & gas intelligence MCP server. "
                "33 tools covering 21,000+ data-center facilities (170+ countries), "
                "2,000+ tracked M&A deals.")
AWESOME_FOREIGN = "- [someone/else](https://github.com/someone/else) - 12 tools. Pro: $19/mo."


def _awesome_reg():
    return next(r for r in w._REGISTRIES if r["id"] == "awesome_mcp_servers")


def test_the_real_mcpso_copy_is_stale():
    out = w._copy_check(MCPSO, CANON, PRO)
    assert out["state"] == "stale", out
    keys = {d["key"] for d in out["stale"]}
    assert "facilities" in keys, out            # 12,650 vs 24,500: -48%
    assert "pro_usd_month" in keys, out         # $299 vs $99


def test_price_mismatch_alone_is_stale():
    """A price is what a buyer is quoted: ANY mismatch is stale, no band."""
    # $109 is inside the 20% count band: only the exact-price rule catches it.
    out = w._copy_check("Pro: $109/mo", CANON, PRO)
    assert out["state"] == "stale" and out["stale"][0]["key"] == "pro_usd_month", out


def test_current_copy_reads_current():
    out = w._copy_check(SMITHERY, CANON, PRO)
    assert out["state"] == "current", out
    assert out["checked"] >= 3, out


def test_small_lag_is_not_stale():
    """Floors rise weekly and registries re-crawl on their own cadence; a 1-tool
    / 6.5% lag must not mark a listing stale or the badge flags everything."""
    out = w._copy_check(GLAMA, CANON, PRO)
    assert out["state"] == "current", out
    assert {d["key"] for d in out["immaterial"]} >= {"tools", "facilities"}, out


def test_no_figures_is_unknown_not_current():
    assert w._copy_check("DC Hub. Data center intelligence.", CANON, PRO)["state"] == "unknown"


def test_unreadable_canon_is_unknown_not_current():
    assert w._copy_check(MCPSO, None, PRO)["state"] == "unknown"
    assert w._copy_check(MCPSO, {"ok": False}, PRO)["state"] == "unknown"


def test_awesome_scope_is_our_line_only():
    body = AWESOME_FOREIGN + "\n" + AWESOME_OURS + "\n" + AWESOME_FOREIGN
    scoped = w._copy_scope(_awesome_reg(), body)
    assert "someone/else" not in scoped
    out = w._copy_check(scoped, CANON, PRO)
    assert out["state"] == "stale", out
    assert any(d["key"] == "tools" and d["listed"] == 33 for d in out["stale"]), out
    # The foreign "Pro: $19/mo" must never be read as OUR price.
    assert not any(d["key"] == "pro_usd_month" for d in out["stale"] + out.get("immaterial", [])), out


def test_probe_all_attaches_copy_and_endpoint_lists_stale(monkeypatch):
    bodies = {
        "https://mcp.so/servers/dchub-mcp-server": MCPSO + " dchub",
        "https://smithery.ai/servers/azmartone67/dchub": SMITHERY + " dchub",
    }
    regs = [r for r in w._REGISTRIES if r["url"] in bodies]
    assert len(regs) == 2, "fixture URLs drifted from _REGISTRIES"
    monkeypatch.setattr(w, "_REGISTRIES", regs)
    monkeypatch.setattr(w, "_fetch", lambda url, timeout=15: (200, bodies[url], url))
    monkeypatch.setattr(w, "_copy_canon", lambda: (CANON, PRO))
    res = w._probe_all()
    assert res["mcp_so"]["verdict"] == "present"
    assert res["mcp_so"]["copy"]["state"] == "stale", res["mcp_so"]
    assert res["smithery_server"]["copy"]["state"] == "current", res["smithery_server"]

    monkeypatch.setattr(w, "_probe_all_cached", lambda force=False: res)
    monkeypatch.setattr(w, "_probed_at_iso", lambda: "2026-09-24T00:00:00Z")
    monkeypatch.setattr(w, "_durable_probed_at", lambda: None)
    from flask import Flask
    app = Flask(__name__)
    with app.test_request_context():
        body = w.mcp_registries_status().get_json()
    assert body["present"] == 2, "`present` keeps its old meaning"
    assert body["present_stale_copy"] == ["mcp_so"], body["present_stale_copy"]


def test_non_present_carries_no_copy_verdict(monkeypatch):
    regs = [r for r in w._REGISTRIES if r["id"] == "mcp_so"]
    monkeypatch.setattr(w, "_REGISTRIES", regs)
    monkeypatch.setattr(w, "_fetch", lambda url, timeout=15: (522, "", url))
    monkeypatch.setattr(w, "_copy_canon", lambda: (CANON, PRO))
    assert w._probe_all()["mcp_so"]["copy"] is None
