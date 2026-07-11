"""Issue #1551 regression guards — conversions must be VISIBLE to the brain,
and metered 402s must coach agents.

Source-pinned (deliberately no imports of main or routes.* — pre-merge CI has
no DB/JWT_SECRET and unit tests must never import main):

  1. brain_consistency_radar._platform_conversions_30d must read the canonical
     Stripe-webhook ledger (mcp_conversions, excluding is_test rows) and never
     the dead pair-code redemption path. 0 of the 1,097 pair codes ever minted
     were redeemed, so the old query pinned the r88 honesty gate at 0 while
     real revenue landed in mcp_conversions (9 non-test rows/30d at the time)
     — producing the false 'mcp_funnel_leak: 446 callers, ZERO conversions'
     alarm that issue #1551 was auto-filed from.

  2. free_tier_gate._metered_402 (the map-session-cap wall that fronts metered
     API surfaces like /api/v1/grid/intelligence/* for anonymous callers) must
     carry the shared agent coaching (build_agent_coaching -> claim_free_key)
     so a walled AI agent has a machine-readable upgrade path, and must keep
     the utm-tagged pricing URL for attribution.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _fn_source(rel_path: str, fn_name: str) -> str:
    src = open(os.path.join(ROOT, rel_path), encoding="utf-8").read()
    m = re.search(rf"\ndef {re.escape(fn_name)}\(.*?(?=\ndef |\Z)", src, re.S)
    assert m, f"{fn_name}() not found in {rel_path}"
    return m.group(0)


def test_platform_conversion_gate_reads_canonical_ledger():
    body = _fn_source("routes/brain_consistency_radar.py",
                      "_platform_conversions_30d")
    assert "mcp_conversions" in body, (
        "_platform_conversions_30d must count the canonical Stripe-webhook "
        "ledger (mcp_conversions) — anything else makes real conversions "
        "invisible to the funnel-leak honesty gate (issue #1551)")
    assert "mcp_pair_codes" not in body, (
        "issue #1551 regression: pair-code redemptions are a dead funnel "
        "(0 of 1,097 minted codes ever redeemed) — counting them pins the "
        "honesty gate at 0 and re-creates the false "
        "'N paywalled callers, ZERO conversions' alarm")
    assert "is_test" in body, (
        "the 30d conversion count must exclude is_test rows or QA canary "
        "purchases will suppress genuine broken-funnel findings")


def test_metered_402_carries_agent_coaching():
    body = _fn_source("free_tier_gate.py", "_metered_402")
    assert "build_agent_coaching" in body, (
        "issue #1551 regression: the map-session-cap 402 must attach the "
        "shared agent coaching (routes.email_capture.build_agent_coaching) "
        "so an anonymous agent walled on a metered API surface gets a "
        "machine-readable claim_free_key path, not just a human pricing URL")
    assert "utm_source=map_session_cap" in body, (
        "the 402 must keep the utm-tagged pricing URL so paywall-driven "
        "checkouts stay attributable to the map-session cap")
