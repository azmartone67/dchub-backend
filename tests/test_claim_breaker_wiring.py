"""tests/test_claim_breaker_wiring.py — the callers honour the gate (step 3).

Proves the two fail-closed wiring points act on the breaker's verdict, with the
breaker STUBBED so the wiring is tested independently of the gate's own logic:

  (a) content_publisher._should_skip_publish (media) — refuse when the gate is
      TRUSTED and not ok; ship (log) when UNTRUSTED; ship when clean.
  (b) white_glove_propagation._paste_ready_block (canon copy) — withhold flagged
      paste-ready copy and substitute a canon-clean line; keep clean copy.

DB-free (cur=None / stubbed builder), never imports main.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

cp = pytest.importorskip("content_publisher")  # noqa: E402


# A clean analyst post that clears every gate BEFORE the breaker (quality is
# stubbed so scoring cannot be the thing that skips it); platform 'twitter' so
# the LinkedIn-only number-lead gate does not apply.
CLEAN = ("6.5 GW moved in PJM's interconnection queue this week, per DC Hub's "
         "latest snapshot.\n\nhttps://dchub.cloud/markets")


@pytest.fixture()
def quality_open(monkeypatch):
    monkeypatch.setattr(cp, "_quality_score", lambda *a, **k: 1.0)


def test_publish_skipped_when_breaker_is_trusted_and_not_ok(monkeypatch, quality_open):
    monkeypatch.setattr(cp, "_run_claim_breaker", lambda text, kind: {
        "ok": False, "trusted": True, "disabled": False,
        "violations": [{"cls": "rows_ne_buildings", "detail": "26,000 vs 18,406"}]})
    skip, why = cp._should_skip_publish(None, CLEAN, "twitter")
    assert skip is True
    assert "claim-breaker" in why and "rows_ne_buildings" in why


def test_publish_proceeds_when_breaker_is_untrusted(monkeypatch, quality_open):
    """UNTRUSTED (its control failed) -> log + ship, never block."""
    monkeypatch.setattr(cp, "_run_claim_breaker", lambda text, kind: {
        "ok": True, "trusted": False, "disabled": False,
        "violations": [{"cls": "rows_ne_buildings", "detail": "x"}]})
    skip, why = cp._should_skip_publish(None, CLEAN, "twitter")
    assert skip is False


def test_publish_proceeds_when_breaker_is_clean(monkeypatch, quality_open):
    """Control: a trusted, ok verdict does not add a skip."""
    monkeypatch.setattr(cp, "_run_claim_breaker", lambda text, kind: {
        "ok": True, "trusted": True, "disabled": False, "violations": []})
    skip, why = cp._should_skip_publish(None, CLEAN, "twitter")
    assert skip is False


def test_publish_proceeds_when_breaker_unavailable(monkeypatch, quality_open):
    """Fail-OPEN: an exception in the gate never dark-holds the publisher."""
    def _boom(text, kind):
        raise RuntimeError("breaker import blew up")
    monkeypatch.setattr(cp, "_run_claim_breaker", _boom)
    skip, why = cp._should_skip_publish(None, CLEAN, "twitter")
    assert skip is False


# ── (b) white-glove registry copy ───────────────────────────────────────────

def test_paste_ready_withholds_flagged_copy(monkeypatch):
    """A generated description that over-claims is withheld and replaced with a
    canon-clean line — never handed to an operator to paste."""
    wgp = pytest.importorskip("routes.white_glove_propagation")
    import routes.mcp_presence_crawler as pc
    bad = "DC Hub tracks " + "50," + "000 facilities in its index right now."
    monkeypatch.setattr(pc, "_build_canonical_description", lambda name: bad)
    block = wgp._paste_ready_block(
        "smithery", {"listing_url": "http://x"}, {"tools": 82},
        [{"kind": "tools", "found": "58", "expected": "82"}])
    assert "50,000" not in block, "flagged copy must not be pasted"
    assert "Real-time, versioned, cited." in block, "safe fallback must be used"


def test_paste_ready_keeps_clean_copy(monkeypatch):
    """Control: a clean description is passed through unchanged."""
    wgp = pytest.importorskip("routes.white_glove_propagation")
    import routes.mcp_presence_crawler as pc
    good = "DC Hub: 82 MCP tools for data-center infrastructure."
    monkeypatch.setattr(pc, "_build_canonical_description", lambda name: good)
    block = wgp._paste_ready_block(
        "smithery", {"listing_url": "http://x"}, {"tools": 82},
        [{"kind": "tools", "found": "58", "expected": "82"}])
    assert good in block
