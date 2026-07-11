"""r-variant-honest-split (2026-07-11) — pure-function tests.

Regression context: the funnel-health "Claim copy A/B by platform" card
showed generic 555 minted / 551 used (99.3%) vs claude 8 / 0 (0%) and read
as a copy defect. It was a cohort artifact: 'used' conflated the server-side
MACHINE auto-redeem (server.mjs _autoRedeemClaim, claim_email IS NULL,
restored 2026-07-04, fires ~1s after mint for gateway agents) with HUMAN
form-submits on /claim/<token> (claim_email IS NOT NULL). All 8 live claude
mints (06-11 → 06-22) predate the auto-redeem restore AND Claude.ai/desktop
are header-less hosts whose only redemption path is a human click — 0 human
email submissions in 30d across ALL variants.

These tests pin the split so 'used' can never again masquerade as a copy
signal. No Flask app context, no DB — module import only.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from routes.mcp_high_intent_claim import (  # noqa: E402
    VALID_VARIANTS,
    _norm_variant,
    _variant_conversion_row,
)


def test_norm_variant_known_platforms():
    for v in ("claude", "cursor", "cline", "chatgpt", "generic"):
        assert _norm_variant(v) == v
    assert _norm_variant(" Claude ") == "claude"


def test_norm_variant_unknown_falls_back_to_generic():
    for junk in ("windsurf", "copilot", "", None, "CLAUDE.AI", "mcp", "node"):
        assert _norm_variant(junk) == "generic"


def test_row_used_is_sum_of_both_channels():
    row = _variant_conversion_row("generic", 555, 551, 0, 4, 0)
    assert row["used"] == 551
    assert row["used_agent"] == 551
    assert row["used_human"] == 0
    assert row["opened"] == 4


def test_machine_autoredeem_does_not_inflate_human_rate():
    # The exact live artifact: generic's 99.3% "use rate" was 100% machine.
    generic = _variant_conversion_row("generic", 555, 551, 0, 4, 0)
    claude = _variant_conversion_row("claude", 8, 0, 0, 0, 0)
    assert generic["use_rate_pct"] == 99.28          # legacy raw rate survives
    assert generic["human_use_rate_pct"] == 0.0      # the honest A/B signal
    assert claude["human_use_rate_pct"] == 0.0
    # On the honest metric the two variants are indistinguishable — no
    # "winner" may be declared from machine redemptions.
    assert generic["human_use_rate_pct"] == claude["human_use_rate_pct"]


def test_human_redemption_moves_the_honest_rate():
    row = _variant_conversion_row("claude", 8, 0, 2, 3, 1)
    assert row["used"] == 2
    assert row["used_human"] == 2
    assert row["human_use_rate_pct"] == 25.0
    assert row["paid_rate_pct"] == 12.5


def test_zero_minted_yields_zero_rates_not_division_error():
    row = _variant_conversion_row("cline", 0, 0, 0, 0, 0)
    assert row["use_rate_pct"] == 0.0
    assert row["human_use_rate_pct"] == 0.0
    assert row["paid_rate_pct"] == 0.0


def test_row_defaults_none_counts_to_zero():
    row = _variant_conversion_row("chatgpt", None, None, None, None, None)
    assert row["minted"] == 0 and row["used"] == 0


def test_valid_variants_frozen_vocabulary():
    # server.mjs claimVariantFromCtx + the dashboard zero-row loop both key
    # on this exact set; a rename breaks attribution joins silently.
    assert VALID_VARIANTS == {"claude", "cursor", "cline", "chatgpt", "generic"}
