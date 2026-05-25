"""
stripe_direct_upgrade.py — direct Stripe checkout for paywall hits.

Phase ZZZZZ-round37 (2026-05-24). 3,477 paywall signals → 0 conversions
in 30d. Brain raised paywall_click_leak_critical because the MCP
paywall response embeds upgrade URLs that landed users on
dchub.cloud/ai#pricing (no Stripe button) or 401-locked pages.

routes/email_capture.py already has hardcoded Stripe payment links
for free/starter/developer/pro/enterprise tiers — they work. This
module wires /pricing/upgrade?tool=X (and aliases) directly to those
URLs based on which tool the user hit the paywall on. No JS, no
form, no redirect chain — 302 straight to Stripe checkout with
client_reference_id baked in for attribution.

Endpoints:
  GET /pricing/upgrade?tool=X        → 302 to Stripe (developer tier by default)
  GET /pricing/upgrade?tier=pro      → 302 to pro Stripe URL
  GET /upgrade?tool=X                → alias
  GET /api/v1/paywall/checkout       → JSON {checkout_url, client_ref}
"""
import os
from urllib.parse import quote
from flask import Blueprint, request, redirect, jsonify

stripe_direct_bp = Blueprint("stripe_direct_upgrade", __name__)

# Source of truth: routes/email_capture.py:STRIPE_LINKS (Phase r36-shipped tier links)
STRIPE_LINKS = {
    "starter":    "https://buy.stripe.com/8x2dRa5sS0x75uteGuaZi0g",
    "developer":  "https://buy.stripe.com/7sY5kE8F4fs13ml0PEaZi0c",
    "pro":        "https://buy.stripe.com/dRm7sM6wW7Zz1edgOCaZi07",
    "enterprise": "https://buy.stripe.com/fZueVe5sS6Vv7CB41QaZi0a",
}

# Tool → recommended tier mapping (which tier is needed to call this tool)
TOOL_TIER_MAP = {
    # Tools gated at Pro
    "get_intelligence_index": "pro",
    "compare_sites":          "pro",
    "analyze_site":           "pro",
    "get_infrastructure":     "pro",
    "get_fiber_intel":        "pro",
    "get_grid_intelligence":  "pro",
    # Tools gated at Developer (fields truncated on free)
    "search_facilities":      "developer",
    "list_transactions":      "developer",
    "get_news":               "developer",
    "get_pipeline":           "developer",
    # New tier-1 tools
    "rank_markets":           "developer",
    "find_alternatives":      "developer",
    "score_facility":         "developer",
    # AI capex relevance tools (r36)
    "ai_capacity_index":      "developer",
    "hyperscaler_deals":      "developer",
}


def _resolve_tier(tool, tier_param):
    """Pick the right Stripe URL — explicit tier wins, else look up tool, else developer."""
    if tier_param and tier_param.lower() in STRIPE_LINKS:
        return tier_param.lower()
    if tool and tool in TOOL_TIER_MAP:
        return TOOL_TIER_MAP[tool]
    return "developer"  # safe default for self-serve


def _build_url(tier, tool, ref):
    base = STRIPE_LINKS[tier]
    # client_reference_id appends as URL param; Stripe webhooks see it
    ref_str = f"mcp:tool={tool or 'none'}:ref={ref or 'paywall'}"
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}client_reference_id={quote(ref_str)}"


@stripe_direct_bp.route("/pricing/upgrade", methods=["GET"], strict_slashes=False)
@stripe_direct_bp.route("/upgrade", methods=["GET"], strict_slashes=False)
def upgrade_redirect():
    tool   = (request.args.get("tool") or "").strip()
    tier   = (request.args.get("tier") or "").strip()
    ref    = (request.args.get("ref")  or "paywall").strip()
    chosen = _resolve_tier(tool, tier)
    url    = _build_url(chosen, tool, ref)
    return redirect(url, code=302)


@stripe_direct_bp.route("/api/v1/paywall/checkout", methods=["GET"])
def paywall_checkout_json():
    """JSON variant so MCP paywall responses can embed a one-click link
    AND show the user the destination before they click."""
    tool   = (request.args.get("tool") or "").strip()
    tier   = (request.args.get("tier") or "").strip()
    ref    = (request.args.get("ref")  or "mcp-paywall").strip()
    chosen = _resolve_tier(tool, tier)
    return jsonify({
        "tool":           tool or None,
        "tier":           chosen,
        "checkout_url":   _build_url(chosen, tool, ref),
        "stripe_managed": True,
        "tier_pricing":   {
            "developer": "$49/mo",
            "pro":       "$199/mo",
            "starter":   "$19/mo",
            "enterprise": "Custom",
        }.get(chosen, "—"),
        "client_reference_id": f"mcp:tool={tool or 'none'}:ref={ref}",
    }), 200, {"Cache-Control": "public, max-age=300"}


@stripe_direct_bp.route("/api/v1/paywall/health", methods=["GET"])
def health():
    return jsonify({
        "blueprint": "stripe_direct_bp",
        "tools_mapped": len(TOOL_TIER_MAP),
        "tiers_available": list(STRIPE_LINKS.keys()),
        "phase": "ZZZZZ-round37",
    }), 200
