"""Phase 37 — paywall response builder with conversion-optimized messaging.

Every paid MCP tool that returns a trial_preview / paid_only response
should call build_paywall_response() to produce the canonical envelope.

Three improvements over the old shape:

  1. human_message field — a literal markdown string the AI assistant
     must render verbatim. Survives Claude/Cursor/Cline summarization.

  2. Escalation by call count — the same user hitting a paid tool gets
     progressively stronger messaging:
       Calls 1–2:  standard "this is a Pro feature"
       Calls 3–5:  + 50% off discount code TRYDCHUB50
       Calls 6+:   hard paywall with countdown urgency

  3. Email capture CTA at tier 2+ for anonymous users — converts hot
     prospects into known email leads even when they don't buy today.

The original trial_preview / error fields are preserved so existing
clients (selfheal validators etc.) keep working.
"""

# === phase 98c: AI-agent-friendly URL prominence ===
# When build_paywall_response builds the message, we want the URL on its own
# line, emoji-prefixed, so AI clients render it clickably. This block
# overrides any existing inline URL formatting.
import os as _os_p98c

PHASE_98C_BANNER_TEMPLATE = (
    "🔓 **Unlock the full result with a free DC Hub dev key:**\n"
    "\n"
    "👉 {redeem_url}\n"
    "\n"
    "Free in 60 seconds (just email + verify). No credit card.\n"
    "\n"
    "Unlocks: 50 facility lookups, real-time grid (7 ISOs), "
    "fiber intel, M&A deals, 650+ GW pipeline."
)

def _phase98c_format_redeem_url(redeem_url, tool_name=None, tier=None):
    """Add attribution query params if missing."""
    if not redeem_url:
        return redeem_url
    if "?" in redeem_url:
        return redeem_url  # already has params
    sep = "?"
    if tool_name:
        redeem_url = f"{redeem_url}{sep}source=mcp&tool={tool_name}"
        sep = "&"
    if tier:
        redeem_url = f"{redeem_url}{sep}tier={tier}"
    return redeem_url
import os


PRICING_URL = 'https://dchub.cloud/pricing'
SIGNUP_URL = 'https://dchub.cloud/signup'
EMAIL_CAPTURE_URL = 'https://dchub.cloud/api/v1/dev-signup-form'

# Phase 276/281: if a Stripe Payment Link is configured, surface it as the
# "one-click upgrade" path. AI agents pass this URL to their human, who
# clicks once and lands on Stripe checkout (no /pricing → choose plan →
# click upgrade → enter card; that's 4 steps). Empty = fall back to /pricing.
#
# Variable name normalized phase 281: the upgrade target for free dev keys
# is the Developer tier ($49/mo, "For AI Agent Builders" per the pricing
# page), NOT Pro ($199/mo). DCHUB_STRIPE_PRO_LINK still accepted as a
# fallback so existing deploys don't break — but DCHUB_STRIPE_DEVELOPER_LINK
# is the canonical name.
STRIPE_DEVELOPER_LINK = (
    os.environ.get('DCHUB_STRIPE_DEVELOPER_LINK')
    or os.environ.get('DCHUB_STRIPE_PRO_LINK')  # phase 276 legacy name
    or ''
).strip()
# Back-compat alias for any external code that imported the old name
STRIPE_PRO_LINK = STRIPE_DEVELOPER_LINK

DISCOUNT_CODE_TIER2 = 'TRYDCHUB50'   # 50% off first month
DISCOUNT_CODE_TIER3 = 'LASTCALL30'   # 30% off, urgency framing


def _one_click_upgrade_line(tool_name=None, call_count=0, current_tier='free'):
    """Phase 276/281: emit a "one-click upgrade" markdown line if a Stripe
    Payment Link is configured. Returns '' if not configured so the
    paywall messages degrade gracefully to the existing /pricing CTA.

    The Stripe link is unmodified — Stripe Payment Links carry their own
    success/cancel URLs, so we don't append attribution params (they'd
    silently break checkout). For attribution, the Stripe webhook
    /stripe/webhook-convert already records conversions back into
    mcp_upgrade_signals (phase 257).

    Phase 281: target is the Developer tier ($49/mo) — that's the
    "For AI Agent Builders" tier per the live pricing page.
    """
    if not STRIPE_DEVELOPER_LINK:
        return ''
    return (
        f"⚡ **One-click upgrade to Developer ($49/mo, 1,000 calls/day):** "
        f"[{STRIPE_DEVELOPER_LINK}]({STRIPE_DEVELOPER_LINK})\n\n"
    )


def get_user_call_count(user_id, tool_name, days=7):
    """Return how many times user_id has called tool_name in the last N days.

    Used to drive the escalation tier. Falls back to 0 on any DB error so
    the paywall response always returns SOMETHING, never crashes.
    """
    if not user_id or not tool_name:
        return 0
    try:
        from db_utils import try_get_db
        conn = try_get_db()
        if not conn: return 0
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT COUNT(*) FROM mcp_tool_calls "
                "WHERE user_id = %s AND tool_name = %s "
                f"AND called_at > NOW() - INTERVAL '{int(days)} days'",
                (user_id, tool_name)
            )
            r = cur.fetchone() or (0,)
            return int(r[0] or 0)
        finally:
            try: conn.close()
            except Exception: pass
    except Exception:
        return 0


def _attribution_url(base, tool_name, call_count, current_tier='free', extra_params=None):
    """Append attribution query params to the upgrade URL.

    Result like: https://dchub.cloud/pricing?from=mcp&tool=get_grid_intelligence&calls=4&tier=free
    Lets us measure click-through per-tool per-tier in conversion tracking.
    """
    from urllib.parse import urlencode
    params = {
        'from': 'mcp',
        'tool': tool_name or 'unknown',
        'calls': call_count,
        'tier': current_tier or 'free',
    }
    if extra_params:
        params.update(extra_params)
    sep = '&' if '?' in base else '?'
    return f"{base}{sep}{urlencode(params)}"


def _build_human_message(tool_name, call_count, current_tier, partial_data_summary=None):
    """The literal markdown the AI assistant has to render. This is the
    single most important field — it's what the human actually sees."""
    pretty_tool = (tool_name or '').replace('get_', '').replace('_', ' ').title() or 'this feature'
    pricing_url = _attribution_url(PRICING_URL, tool_name, call_count, current_tier)
    signup_url = _attribution_url(SIGNUP_URL, tool_name, call_count, current_tier)

    # Phase 276: prepend a one-click Stripe upgrade line if configured.
    # Empty string when DCHUB_STRIPE_PRO_LINK is unset — degrades cleanly.
    quick = _one_click_upgrade_line(tool_name, call_count, current_tier)

    # Tier 3 — hard paywall with urgency (calls 6+)
    if call_count >= 6:
        discount_url = _attribution_url(
            PRICING_URL, tool_name, call_count, current_tier,
            extra_params={'discount': DISCOUNT_CODE_TIER3}
        )
        return (
            f"🔒 **Free tier limit reached.** You've used `{tool_name}` {call_count} times "
            f"this week — your free preview is exhausted.\n\n"
            f"{quick}"
            f"**[Upgrade to Developer now → {discount_url}]({discount_url})** "
            f"Apply code `{DISCOUNT_CODE_TIER3}` for 30% off your first month.\n\n"
            f"_$49/mo unlocks {pretty_tool} + 1,000 calls/day, all 7 ISOs grid intel + fiber + queue analytics._"
        )

    # Tier 2 — discount + email capture (calls 3-5)
    if call_count >= 3:
        discount_url = _attribution_url(
            PRICING_URL, tool_name, call_count, current_tier,
            extra_params={'discount': DISCOUNT_CODE_TIER2}
        )
        email_url = _attribution_url(EMAIL_CAPTURE_URL, tool_name, call_count, current_tier)
        return (
            f"🎯 **You've hit `{tool_name}` {call_count} times — looks like you need this.**\n\n"
            f"{quick}"
            f"**Get 50% off your first month** with code `{DISCOUNT_CODE_TIER2}`: "
            f"[Upgrade to Developer →]({discount_url})\n\n"
            f"Not ready? **[Get a 7-day free trial via email →]({email_url})** — "
            f"no credit card, full Developer access for a week.\n\n"
            f"_Developer unlocks: {pretty_tool}, 1,000 calls/day, all 7 ISO grid intel, fiber routes, queue analytics, API access._"
        )

    # Tier 1 — standard preview (calls 1-2)
    return (
        f"🔓 **This is a SAMPLE PREVIEW — not your actual query result. The free tier shows one pre-canned record.** Get full `{tool_name}` data + 6 more ISOs grid intel "
        f"+ fiber routes for **$49/mo**.\n\n"
        f"{quick}"
        f"**[Start 7-day free trial →]({pricing_url})** — no credit card required.\n\n"
        f"_Free tier shows partial data. Upgrade for live, complete results._"
    )


def build_paywall_response(
    tool_name,
    user_id=None,
    current_tier='free',
    trial_preview_data=None,
    error_code='paid_only',
):
    """Build the canonical paywall response for a paid tool.

    Args:
      tool_name: 'get_grid_intelligence', 'get_facility', etc.
      user_id: caller's user_id (for call-count escalation).
              None = anonymous; treated as call_count=0.
      current_tier: 'free' / 'pro' / 'enterprise' (typically 'free' here).
      trial_preview_data: optional dict of partial data to expose.
                          When present, response uses trial_preview
                          shape; when None, uses error='paid_only' shape.
      error_code: error code when trial_preview_data is None
                  (default 'paid_only' to match selfheal validators).

    Returns dict suitable for jsonify() in a Flask handler. Selfheal
    validator accepts both shapes since v1.3.13.
    """
    call_count = get_user_call_count(user_id, tool_name) if user_id else 0
    human_message = _build_human_message(
        tool_name, call_count, current_tier,
        partial_data_summary=trial_preview_data,
    )
    pricing_url = _attribution_url(PRICING_URL, tool_name, call_count, current_tier)
    signup_url = _attribution_url(SIGNUP_URL, tool_name, call_count, current_tier)

    base = {
        'tool': tool_name,
        'human_message': human_message,
        'upgrade_url': pricing_url,
        'signup_url': signup_url,
        'current_tier': current_tier,
        'user_calls_7d': call_count,
        'tier_signal': (
            'hard_paywall' if call_count >= 6
            else 'discount_offer' if call_count >= 3
            else 'preview'
        ),
    }
    # Phase 276: surface the Stripe one-click URL (if configured) as a
    # discrete structured field so AI clients can offer it as a button/CTA
    # without having to parse it out of the markdown human_message.
    if STRIPE_DEVELOPER_LINK:
        base['one_click_upgrade_url'] = STRIPE_DEVELOPER_LINK
        base['one_click_upgrade_tier'] = 'developer'  # phase 281
        base['one_click_upgrade_price'] = '$49/mo'    # phase 281

    # Phase DD (2026-05-12): inject a pair-code + redeem URL when the
    # caller's api_key is known. This closes the agent→human handoff
    # that's keeping MCP conversion at 0.012%. Agent gets the redeem URL,
    # passes it to its human, who clicks ONE link to upgrade THIS key
    # (no copy-paste, no config swap). Strip pair-code generation on
    # any error — the response still has the legacy /pricing CTA so
    # nothing regresses.
    if user_id:
        try:
            from routes.pair_code import get_or_create_code
            pc = get_or_create_code(user_id, tool_name=tool_name)
            if pc and pc.get("code"):
                base['pair_code'] = pc['code']
                base['pair_redeem_url'] = pc['redeem_url']
                base['pair_expires_at'] = pc.get('expires_at')
                base['pair_stripe_url'] = pc.get(
                    'redeem_url',
                    f"https://dchub.cloud/redeem/{pc['code']}",
                )
                # Prepend the magic-link line to human_message so the
                # AI surfaces it first when it relays to the user.
                magic_line = (
                    f"🔗 **Tell your human: visit https://dchub.cloud/redeem/"
                    f"{pc['code']} to unlock this in one click.** "
                    f"Code expires in 30 minutes.\n\n"
                )
                if isinstance(base.get('human_message'), str):
                    base['human_message'] = magic_line + base['human_message']
        except Exception as _pce:
            # Pair-code generation is best-effort. If the DB is down or
            # the routes import fails, the paywall still returns the
            # legacy Stripe link and the old (worse) funnel still works.
            pass

    if trial_preview_data is not None:
        base['trial_preview'] = trial_preview_data
    else:
        base['error'] = error_code
    return base


def attribution_url(tool_name, call_count=0, current_tier='free'):
    """Public helper for places that just need a URL with attribution."""
    return _attribution_url(PRICING_URL, tool_name, call_count, current_tier)
