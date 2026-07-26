"""Retention wave (2026-07-26) — pins the claim-carry + measurement contracts.

The wave: (1) the mcp-server's session→key binding is in-memory per replica,
so claimed identities died on rotation — a backend session-key endpoint now
lets the server re-adopt them (live tier, not just telemetry); (2) the
r-return hook finally gets MEASURED (hooked vs un-hooked return rates) before
the durable-identity spend; (3) the per-platform funnel separates callers
from crawlers so a platform push is judged on adoption, not crawl volume.

CI-SAFETY: no DATABASE_URL in the unit env — direct imports, fail-soft
contracts, source-text wiring pins. Never import main.
"""
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


# ── session-key restore endpoint ─────────────────────────────────────

def test_session_key_endpoint_exists_and_is_internal_gated():
    src = _read("flask_mcp_endpoints.py")
    assert '@mcp_bp.get("/api/v1/mcp/session-key")' in src
    i = src.index('def mcp_session_key')
    body = src[i:i + 1600]
    assert "X-Internal-Key" in body, "must be internal-key gated"
    assert "no-store" in body, "identity responses must never edge-cache"
    assert "_resolve_session_claimed_key" in body, \
        "must reuse the cached resolver (hot path = one dict hit)"


def test_session_key_endpoint_fails_closed_without_internal_key():
    # The gate must require a configured INTERNAL_KEY — an empty expected
    # value can never authenticate (not INTERNAL_KEY → 401).
    src = _read("flask_mcp_endpoints.py")
    i = src.index('def mcp_session_key')
    assert "if not INTERNAL_KEY or _sent != INTERNAL_KEY" in src[i:i + 800]


# ── r-return hook measurement ────────────────────────────────────────

def test_flywheel_measures_the_return_hook():
    src = _read(os.path.join("routes", "flywheel_master_shell.py"))
    assert "ret_return_hook" in src
    assert "get_changes" in src
    assert "is_real_external IS TRUE" in src, \
        "hook cohorts must use the canonical crawler-free basis"


# ── per-platform funnel ──────────────────────────────────────────────

def test_platform_funnel_registered_and_no_store():
    assert "register_blueprint(platform_funnel_bp)" in _read("main.py")
    src = _read(os.path.join("routes", "platform_funnel.py"))
    assert "no-store" in src
    assert "is_real_external IS TRUE" in src, \
        "the funnel exists to exclude crawlers — identity basis only"
    assert "claim_free_key" in src


def test_platform_funnel_failsoft_without_db(monkeypatch):
    pytest.importorskip("flask")
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from routes import platform_funnel as pf
    monkeypatch.setattr(pf, "_db", lambda: None)
    out = pf._compute()
    assert out.get("ok") is False and "error" in out


def test_platform_funnel_ranks_by_complete_weeks_only():
    # A partial current week must not reorder platforms (the '*' row is
    # excluded from ranking) — pinned as source text on the _rank helper.
    src = _read(os.path.join("routes", "platform_funnel.py"))
    assert 'if not w["partial"]' in src
