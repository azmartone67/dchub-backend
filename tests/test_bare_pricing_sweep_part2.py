"""r-bare-pricing-sweep (2026-09-23), continuing ship item #2 past the
metered-wall fix (tests/test_metered_wall_no_bare_pricing.py). Three more
walls whose upgrade CTA was a hardcoded, unattributable literal.

Source-pinned, never imported: grid_intelligence_routes.py, autopilot_routes.py
and pjm_node_lmp.py each transitively pull in main.py's module-scope side
effects (DB pool init, ~150 blueprint registrations, background threads) —
the same reason tests/test_pjm_dom_source_errors.py and
tests/test_paid_attributed_joins_relayed_checkout.py parse instead of import.
"""
from __future__ import annotations

import ast
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel_path: str) -> str:
    with open(os.path.join(ROOT, rel_path), encoding="utf-8") as f:
        return f.read()


def _fn_source(rel_path: str, fn_name: str) -> str:
    src = _src(rel_path)
    m = re.search(rf"\ndef {re.escape(fn_name)}\(.*?(?=\ndef |\Z)", src, re.S)
    assert m, f"{fn_name}() not found in {rel_path}"
    return m.group(0)


# ── pjm_node_lmp.py: identified-required, not a paid wall ──────────────────
def test_pjm_node_lmp_gives_an_agent_a_machine_readable_claim_path():
    """This gate resolves with ANY key, including a free one — the fix is
    NOT a checkout link (there's no SKU being hidden), it's attaching the
    same claim_free_key coaching every other identified+ wall already gives
    an agent (build_agent_coaching, shared with free_tier_gate's own
    anonymous-map 402)."""
    body = _fn_source("routes/pjm_node_lmp.py", "node_lmp")
    assert "build_agent_coaching" in body
    assert '"pjm_node_lmp"' in body
    assert 'tier_required="identified"' in body


def test_pjm_node_lmp_still_fails_open_to_the_literal_pricing_page():
    """No signing/import machinery needed here (there's nothing to sign),
    so the base payload keeps a plain informational pricing URL — this is
    NOT the false-promise pattern the other two fixes guard against, since
    no purchase resolves this gate faster than a free claim does."""
    body = _fn_source("routes/pjm_node_lmp.py", "node_lmp")
    assert '"upgrade_url": "https://dchub.cloud/pricing"' in body


# ── autopilot_routes.py: _check_tier's required_tier threads straight through ──
def test_check_tier_signs_its_own_required_tier_never_bare_pricing():
    body = _fn_source("routes/autopilot_routes.py", "_check_tier")
    assert "'https://dchub.cloud/pricing'" not in body
    assert body.count("_tier_upgrade_url(required_tier") == 2, body


def test_tier_upgrade_url_binds_the_key_and_signs_through_checkout_url():
    body = _fn_source("routes/autopilot_routes.py", "_tier_upgrade_url")
    assert "from routes.checkout_click_tracker import checkout_url" in body
    assert "key_refs(api_key)" in body
    assert "checkout_url(required_tier, sub_ref)" in body
    # the ONE place a bare literal survives: the fail-open except branch,
    # for the same reason checkout_url() itself fails open — a walled
    # caller must never see a broken link over a worse-but-working one.
    assert body.count("'https://dchub.cloud/pricing'") == 1


def test_the_two_check_tier_call_sites_still_pass_their_own_required_tier():
    """Confirms the fix is genuinely parametric — the enterprise-gated
    routes (stats/pending/config) and the pro-gated one (transactions) each
    keep selling the plan that actually opens THEM, not a hardcoded one."""
    src = _src("routes/autopilot_routes.py")
    assert src.count("_check_tier('enterprise')") >= 3
    assert "_check_tier('pro')" in src


# ── grid_intelligence_routes.py: free→developer, developer→pro ─────────────
def test_grid_intelligence_upgrade_ctas_are_signed_not_bare():
    body = _fn_source("routes/grid_intelligence_routes.py", "get_grid_region")
    assert "https://buy.stripe.com" not in body   # the literal URL, not prose mentioning the domain
    assert "checkout_url('developer', _sub_ref) if checkout_url" in body
    assert "checkout_url('pro', _sub_ref) if checkout_url" in body
    # the bare literals survive ONLY as the ternary's fail-open fallback
    # (checkout_click_tracker import failed) — never the primary CTA.
    assert body.count("https://dchub.cloud/pricing#developer") == 1
    assert body.count("https://dchub.cloud/pricing#pro") == 1


def test_grid_intelligence_checkout_key_kept_as_a_compat_alias():
    """api-response-contract caught this: '_upgrade.checkout' used to carry
    the raw buy.stripe.com link and some consumer outside this repo may
    still read it. Dropping the key silently broke the contract gate;
    restoring the OLD raw link would reintroduce the bug this PR fixes. The
    fix is a compat alias — same signed URL under both keys."""
    body = _fn_source("routes/grid_intelligence_routes.py", "get_grid_region")
    assert "'checkout': _dev_url" in body
    assert "_dev_url = checkout_url('developer', _sub_ref)" in body


def test_grid_intelligence_ctas_only_evaluate_for_the_tiers_that_have_one():
    """api_key is unset on the internal-key (always tier='pro') path — the
    key_refs() call must only ever run for tier in (free, developer), the
    two branches that actually set api_key."""
    body = _fn_source("routes/grid_intelligence_routes.py", "get_grid_region")
    assert "tier in ('free', 'developer')" in body
