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

# r39 (2026-05-25): centralized in routes/_stripe_links.py. Re-export
# locally so existing _resolve_tier callers don't need to change.
from routes._stripe_links import STRIPE_LINKS, TOOL_TIER_MAP, resolve_tier as _resolve_tier


def _build_url(tier, tool, ref):
    base = STRIPE_LINKS[tier]
    # client_reference_id appends as URL param; Stripe webhooks see it
    ref_str = f"mcp:tool={tool or 'none'}:ref={ref or 'paywall'}"
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}client_reference_id={quote(ref_str)}"


@stripe_direct_bp.route("/pricing/upgrade", methods=["GET"], strict_slashes=False)
@stripe_direct_bp.route("/upgrade", methods=["GET"], strict_slashes=False)
def upgrade_redirect():
    """r39: default behavior now routes through email-capture form so we
    identify every paywall-click. Add ?direct=1 to skip the form and go
    straight to Stripe (legacy behavior, kept for testing + power users).
    """
    tool   = (request.args.get("tool") or "").strip()
    tier   = (request.args.get("tier") or "").strip()
    ref    = (request.args.get("ref")  or "paywall").strip()
    direct = (request.args.get("direct") or "").strip() in ("1","true","yes")
    chosen = _resolve_tier(tool, tier)

    # r39: route through email capture for identity gating BEFORE Stripe.
    # /upgrade legacy path keeps the old direct behavior to not break the
    # pair-code redeem flow that was here pre-r38.
    if not direct and request.path.startswith("/pricing/upgrade"):
        from urllib.parse import urlencode
        params = {"tool": tool, "tier": chosen, "ref": ref}
        params = {k: v for k, v in params.items() if v}
        return redirect(f"/pricing/checkout/start?{urlencode(params)}", code=302)

    # Direct path or /upgrade legacy: straight to Stripe
    url = _build_url(chosen, tool, ref)
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
            "starter":   "$9/mo",
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
