"""A registry listing is a published claim with our name on it.

★ PRESENCE IS NOT CURRENCY. `_audit_target` asked one question — is
`audit_signal` still a substring of the page — so it caught DELISTING and
nothing else. Measured 2026-09-20: mcp.so had been serving "12,650+ facilities"
against a canon of 24,400+, and "79 tools" against 91, with a GREEN audit the
whole time; yellowmcp sat at "20,000+". Both were found by hand, which is
exactly what #4883's own comment predicted: a number pasted into a registry
"goes stale where no drift detector of ours can reach it".

These pin the detector that reaches it.
"""
from __future__ import annotations

import ast
import os

from routes.mcp_registry_outreach import _fig_int, _listing_drift

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CANON = {"facilities": "24,400+", "fiber_routes": "58,000+",
         "substations": "127,000+", "transmission_lines": "95,000+",
         "deals": "2,200+"}

# The copy mcp.so was actually serving, verbatim.
MCPSO_REAL = ("Live data-center, grid, fiber & M&A intelligence for AI agents "
              "— 79 tools, 12,650+ facilities.")


def test_the_real_stale_listing_is_caught():
    """★ THE CASE THAT MOTIVATED THIS. If it ever stops firing, the detector
    has stopped reaching the thing it was built for."""
    assert _fig_int("12,650", "") != _fig_int(CANON["facilities"], ""), (
        "guard-the-guard: the fixture equals canon, so this proves nothing")
    out = _listing_drift(MCPSO_REAL, CANON)
    assert out["state"] == "stale", out
    d = [x for x in out["drift"] if x["key"] == "facilities"]
    assert d and d[0]["listed"] == 12650 and d[0]["canon"] == 24400, out
    assert d[0]["direction"] == "stale_under"


def test_a_current_listing_reads_current():
    out = _listing_drift(
        "24,400+ data centers. 58,000+ fiber routes, 127,000+ substations.", CANON)
    assert out["state"] == "current" and out["drift"] == []
    assert out["checked"] == 3, "it must say HOW MANY figures it compared"


def test_k_form_is_understood():
    """Published copy writes the grid layers as '127k substations'. Reading
    that as 127 would report a catastrophic fake drift on every listing."""
    out = _listing_drift("127k substations, 58k fiber routes", CANON)
    assert out["state"] == "current", out
    assert out["checked"] == 2


def test_a_page_with_no_figures_is_UNKNOWN_not_current():
    """★ THREE STATES, NOT TWO. This is the whole point: the audit it replaces
    was already green-on-nothing, and a detector that reports 'current' for a
    page it could not read reproduces that defect one layer up."""
    out = _listing_drift("DC Hub is an MCP server for infrastructure.", CANON)
    assert out["state"] == "unknown", out
    assert out["checked"] == 0
    assert out["state"] != "current"


def test_an_empty_canon_is_UNKNOWN_not_current():
    """Nothing to compare against is not a clean bill of health."""
    out = _listing_drift("24,400+ data centers", canon={})
    assert out["state"] == "unknown", out


def test_a_RAISING_canon_is_UNKNOWN_not_current(monkeypatch):
    """★ Same rule for the other side — and it has to REACH that branch.

    The first version of this test passed `canon={}`, which never enters the
    `canon is None` block where the except lives: it falls through to the
    no-figures path and returns "unknown" for a different reason entirely. The
    mutation "unreadable canon reports clean" SURVIVED it. Only canon=None plus
    a resolver that RAISES exercises the handler being asserted."""
    import ai_surface_canon as asc

    def _boom():
        raise RuntimeError("canon down")

    monkeypatch.setattr(asc, "resolve_public_floors_cached", _boom, raising=True)
    out = _listing_drift("24,400+ data centers", canon=None)
    assert out["state"] == "unknown", out
    assert str(out.get("reason", "")).startswith("canon_unreadable"), out


def test_an_over_claim_is_named_as_one():
    """Both directions are drift; they are not equally bad. A figure ABOVE
    canon is an over-claim published under our name on someone else's site —
    the canonical_floor_above_live_reality class, off our own property."""
    out = _listing_drift("99,000+ fiber routes", CANON)
    assert out["state"] == "stale"
    d = out["drift"][0]
    assert d["direction"] == "over_claim", d
    assert "OVER-CLAIM" in d["detail"]


def test_the_audit_actually_calls_it():
    """★ WIRED, not merely present. A detector nothing invokes detects nothing —
    and the audit is the one place that already holds the page body, so this
    costs no extra request to a third party."""
    src = open(os.path.join(ROOT, "routes", "mcp_registry_outreach.py"),
               encoding="utf-8").read()
    fn = next((n for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.FunctionDef) and n.name == "_audit_target"), None)
    assert fn is not None, "_audit_target is gone — re-point this guard"
    called = {n.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_listing_drift" in called, (
        "_audit_target no longer computes drift — the audit is back to "
        "presence-only, which is how 12,650+ stayed green for weeks")


def test_drift_is_only_reported_when_we_are_listed():
    """Drift on a page that does not list us is not our copy going stale, it is
    us being gone — which `listed` already says. Reporting both would file the
    same delisting twice under two names."""
    src = open(os.path.join(ROOT, "routes", "mcp_registry_outreach.py"),
               encoding="utf-8").read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "_audit_target")
    body = ast.unparse(fn)
    i = body.index("_listing_drift")
    assert "if listed" in body[:i], (
        "the drift call is not guarded by `if listed` — an unlisted page would "
        "be reported as stale copy instead of as a delisting")
