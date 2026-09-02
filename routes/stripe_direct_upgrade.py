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
from routes._stripe_links import (STRIPE_LINKS, TOOL_TIER_MAP, TIER_PRICE_LABEL,
                                  resolve_tier as _resolve_tier)


def _price_label(tier):
    """The published label for `tier`, from the one canon in _stripe_links.

    ★2026-09-02 merge: this arrived with a hand-typed fallback dict
    ({developer: $49/mo, pro: $299/mo, starter: $9/mo, enterprise: Custom}).
    Every one of those four keys is already in TIER_PRICE_LABEL with the same
    value, so the fallback was unreachable — and it was a SECOND copy of the
    prices, which is the exact defect this PR exists to remove (the scan in
    tests/test_agent_surfaces_one_canon.py flags it). One canon, or the two
    drift apart the next time a price moves.
    """
    return TIER_PRICE_LABEL.get(tier) or "—"


def _build_url(tier, tool, ref, surface=None, sid=None):
    base = STRIPE_LINKS[tier]
    # per-surface-attr (2026-06-20): when a WEB surface drove the click
    # (?surface=market|facility|dcpi|pricing|…) emit the parseable
    # web__<surface>__<slug> client_reference_id so the canonical Stripe
    # webhook records mcp_conversions.web_source=<surface>/web_tool=<slug>
    # and the operator can SEE which page sells. `ref` (the page slug) is
    # the <slug>. With no surface this is an agent/MCP paywall hit, so keep
    # the legacy mcp:tool=…:ref=… shape (attributed via the agent funnel).
    if surface:
        from routes._attribution_ref import build_web_ref
        ref_str = build_web_ref(surface, ref)
    else:
        ref_str = f"mcp:tool={tool or 'none'}:ref={ref or 'paywall'}"
        # sid-preserve (2026-07-07): when the paywall carried an Mcp-Session-Id,
        # append :sess=<sid> so the checkout.session.completed webhook binds the
        # grant back to the REAL session (claim→paid attribution + same-session
        # instant unlock). conversion_attribution._TOOL_RE still finds tool= via
        # .search(); this is NOT a reserved DCM-/tu-/ref_/web__ ref. No sid →
        # byte-identical to before.
        if sid:
            import re as _re_sid
            _s = _re_sid.sub(r"[^A-Za-z0-9_.-]", "", str(sid))[:120]
            if _s:
                ref_str = f"{ref_str}:sess={_s}"
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}client_reference_id={quote(ref_str)}"


@stripe_direct_bp.route("/pricing/upgrade", methods=["GET"], strict_slashes=False)
@stripe_direct_bp.route("/upgrade", methods=["GET"], strict_slashes=False)
def upgrade_redirect():
    """r39: default behavior now routes through email-capture form so we
    identify every paywall-click. Add ?direct=1 to skip the form and go
    straight to Stripe (legacy behavior, kept for testing + power users).
    """
    tool    = (request.args.get("tool") or "").strip()
    tier    = (request.args.get("tier") or "").strip()
    ref     = (request.args.get("ref")  or "paywall").strip()
    surface = (request.args.get("surface") or "").strip()  # per-surface-attr
    direct  = (request.args.get("direct") or "").strip() in ("1","true","yes")
    # sid-preserve (2026-07-07): the relay carries the Mcp-Session-Id so the
    # webhook can bind the conversion back to the paywall session (?sid= or the
    # forwarded X-MCP-Session header).
    sid     = (request.args.get("sid")
               or request.headers.get("X-MCP-Session")
               or request.headers.get("Mcp-Session-Id") or "").strip()
    chosen  = _resolve_tier(tool, tier)

    # partner-attr (2026-08-07): with no explicit ?surface=, fall back to the
    # referral cookie set by /r/<partner>, so a reseller-driven signup converts
    # as web__partner__<slug> instead of vanishing into web__pricing__none.
    # An explicit surface still wins, so every existing caller is unchanged.
    # Resolved BEFORE the email-capture hand-off so the forwarded ?surface=
    # carries the partner through that leg too.
    from routes._attribution_ref import PARTNER_COOKIE, resolve_attribution
    surface, ref = resolve_attribution(
        surface, ref, request.cookies.get(PARTNER_COOKIE))

    # r39: route through email capture for identity gating BEFORE Stripe.
    # /upgrade legacy path keeps the old direct behavior to not break the
    # pair-code redeem flow that was here pre-r38.
    if not direct and request.path.startswith("/pricing/upgrade"):
        from urllib.parse import urlencode
        params = {"tool": tool, "tier": chosen, "ref": ref, "surface": surface, "sid": sid}
        params = {k: v for k, v in params.items() if v}
        return redirect(f"/pricing/checkout/start?{urlencode(params)}", code=302)

    # Direct path or /upgrade legacy: straight to Stripe
    url = _build_url(chosen, tool, ref, surface, sid)
    return redirect(url, code=302)


def _default_paid_tier() -> str:
    """The tier a paywall checkout lands on when nothing chose one.

    ★2026-09-02 (QA-sweep pricing 3): the only plan with completed web-direct
    checkouts in the 8-week licences read is Founding Member ($99) — and
    agents never saw it: this endpoint, unlock_more_data and mcp_facts all
    omitted it. While routes/founding_customers.founding_status() says the
    program is open, an unchosen "developer" becomes founding; when the seats
    are gone it is developer again.

    ★MERGE NOTE 2026-09-02: this was written when resolve_tier() fell through
    to "developer". It no longer does — the relay finding moved the
    fall-through to PACK_TIER ("metered", the $10 one-time pack), because a
    caller we cannot classify should be offered the cheapest non-recurring
    thing, not a $49/mo plan. That rule is UNTOUCHED here: "metered" is not
    "developer", so an unclassified caller never reaches this helper. What is
    left is the case this was actually for — a TOOL_TIER_MAP row that names
    "developer" (get_dchub_recommendation and its neighbours) with no explicit
    ?tier=. An explicit ?tier= is never overridden, and a tool mapped to "pro"
    stays pro.
    """
    try:
        from routes.founding_customers import founding_status
        if (founding_status() or {}).get("program_active") and "founding" in STRIPE_LINKS:
            return "founding"
    except Exception:
        pass
    return "developer"


@stripe_direct_bp.route("/api/v1/paywall/checkout", methods=["GET"])
def paywall_checkout_json():
    """JSON variant so MCP paywall responses can embed a one-click link
    AND show the user the destination before they click."""
    tool    = (request.args.get("tool") or "").strip()
    tier    = (request.args.get("tier") or "").strip()
    ref     = (request.args.get("ref")  or "mcp-paywall").strip()
    surface = (request.args.get("surface") or "").strip()  # per-surface-attr
    chosen  = _resolve_tier(tool, tier)
    if not tier and chosen == "developer":
        chosen = _default_paid_tier()      # founding while open — see helper
    # partner-attr (2026-08-07): same cookie fallback as /pricing/upgrade, so
    # the JSON paywall link an agent embeds carries partner attribution too.
    from routes._attribution_ref import PARTNER_COOKIE, resolve_attribution
    surface, ref = resolve_attribution(
        surface, ref, request.cookies.get(PARTNER_COOKIE))
    if surface:
        from routes._attribution_ref import build_web_ref
        _cref = build_web_ref(surface, ref)
    else:
        _cref = f"mcp:tool={tool or 'none'}:ref={ref}"
    return jsonify({
        "tool":           tool or None,
        "tier":           chosen,
        "checkout_url":   _build_url(chosen, tool, ref, surface),
        "stripe_managed": True,
        # 2026-09-02: label from canon so the pack (now the fall-through
        # tier) reads "$10 / 1,000 API calls", not "—".
        "tier_pricing":   _price_label(chosen),
        "client_reference_id": _cref,
    }), 200, {"Cache-Control": "public, max-age=300"}


@stripe_direct_bp.route("/api/v1/paywall/health", methods=["GET"])
def health():
    return jsonify({
        "blueprint": "stripe_direct_bp",
        "tools_mapped": len(TOOL_TIER_MAP),
        "tiers_available": list(STRIPE_LINKS.keys()),
        "phase": "ZZZZZ-round37",
    }), 200
