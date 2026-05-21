# phase62i_ip_fallback -- IP captured via Flask request fallback
"""
mcp_upgrade_gate.py — Paywall, signal-firing, platform detection for MCP.
Wire from dchub_mcp_server.py:

    from mcp_upgrade_gate import gate_tool_call

    @mcp.tool(...)
    def analyze_site(...):
        ctx = mcp.get_request_context()        # adapt to your MCP framework
        ua  = ctx.headers.get("user-agent", "")
        key = ctx.headers.get("x-api-key", "")
        sid = ctx.session_id
        gate = gate_tool_call("analyze_site", api_key=key, user_agent=ua, session_id=sid)
        if not gate["allowed"]:
            return {"error": gate["message"], "upgrade_url": gate["upgrade_url"]}
        # ... existing tool logic
"""
import os
from contextlib import contextmanager

import psycopg

NEON_URL  = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")

PAID_ONLY_TOOLS = {
    "analyze_site",
    "compare_sites",
    "get_grid_intelligence",
    "get_dchub_recommendation",
    "get_fiber_intel",
}

FREE_DAILY_LIMIT = int(os.environ.get("MCP_FREE_DAILY_LIMIT", "100"))
UPGRADE_URL      = os.environ.get("DCHUB_UPGRADE_URL", "https://dchub.cloud/pricing")
SIGNUP_URL       = os.environ.get("DCHUB_SIGNUP_URL",  "https://dchub.cloud/ai")

PLATFORM_MARKERS = [
    ("claude","claude"), ("chatgpt","chatgpt"), ("openai-mcp","chatgpt"),
    ("cursor","cursor"), ("copilot","copilot"), ("gemini","gemini"),
    ("perplexity","perplexity"), ("grok","grok"), ("deepseek","deepseek"),
    ("codex","codex"), ("glama","glama"), ("windsurf","windsurf"),
    ("cohere","cohere"), ("meta","meta"), ("you.com","you"),
    ("curl","curl"), ("postman","postman"),
]

def detect_platform(user_agent: str = "") -> str:
    u = (user_agent or "").lower()
    for marker, name in PLATFORM_MARKERS:
        if marker in u:
            return name
    return "unknown"

@contextmanager
def _cursor():
    if not NEON_URL:
        raise RuntimeError("NEON_DATABASE_URL not set")
    with psycopg.connect(NEON_URL, autocommit=True) as conn, conn.cursor() as cur:
        yield cur

def validate_key_tier(api_key: str = "") -> str:
    """Return 'free', 'paid', or 'enterprise'. Defaults to 'free'."""
    if not api_key:
        return "free"
    try:
        with _cursor() as cur:
            cur.execute(
                "SELECT tier FROM mcp_dev_keys WHERE api_key=%s AND status='active'",
                (api_key,),
            )
            r = cur.fetchone()
            if r:
                return r[0]
    except Exception:
        pass
    return "free"

def fire_upgrade_signal(*, signal_type, tool_requested=None, tier_current="free",
                        tier_required="paid", message_shown=None, mcp_client=None,
                        user_agent=None, daily_usage=None, daily_limit=None,
                        session_id=None, user_email=None, ip_address=None,
                        api_key=None):  # r32-conv-2 (2026-05-20)
    """Insert a row into mcp_upgrade_signals. Never raises — telemetry is fire-and-forget.

    r32-conv-2 (2026-05-20): added api_key kwarg. If user_email wasn't
    passed in but api_key is, resolve email via api_keys → users join
    so the signal row gets the addressable identifier. Closes the
    0.0% email-capture-rate gap that left /upgrade-pool/preview
    returning 0 candidates against 15,826 signals.
    """
    # phase62j_chain_fallback -- pull IP/UA from Flask request scope
    try:
        from flask import request as _req, has_request_context as _hrc
        if _hrc():
            if not ip_address:
                ip_address = ((_req.headers.get('X-Forwarded-For') or '').split(',')[0].strip()
                              or _req.headers.get('Cf-Connecting-Ip')
                              or _req.remote_addr)
            if not user_agent:
                user_agent = _req.headers.get('User-Agent')
            # Also try to lift the api_key from the live request if
            # the caller didn't pass it explicitly.
            if not api_key:
                api_key = (_req.headers.get('X-API-Key') or
                           _req.args.get('api_key'))
                if not api_key:
                    auth = _req.headers.get('Authorization', '')
                    if auth.startswith('Bearer ') and auth[7:].startswith('dchub_'):
                        api_key = auth[7:]
    except Exception:
        pass

    # r32-conv-2: resolve api_key → user email if we have one and
    # caller didn't pass email. This is the forward fix for the
    # 0.0% capture rate.
    if not user_email and api_key and api_key.startswith('dchub_'):
        try:
            import hashlib as _hashlib
            with _cursor() as cur:
                kh = _hashlib.sha256(api_key.encode()).hexdigest()
                cur.execute(
                    """SELECT u.email FROM api_keys ak
                         JOIN users u ON ak.user_id = u.id
                        WHERE ak.key_hash = %s
                          AND COALESCE(ak.is_active, 1) = 1
                        LIMIT 1""",
                    (kh,),
                )
                row = cur.fetchone()
                if row and row[0]:
                    user_email = row[0]
        except Exception:
            pass
    try:
        with _cursor() as cur:
            cur.execute(
                """INSERT INTO mcp_upgrade_signals
                     (session_id, user_email, ip_address, signal_type, tool_requested,
                      tier_current, tier_required, daily_usage, daily_limit,
                      message_shown, mcp_client, user_agent, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())""",
                (session_id, user_email, ip_address, signal_type, tool_requested,
                 tier_current, tier_required, daily_usage, daily_limit,
                 message_shown, mcp_client, user_agent),
            )
    except Exception as e:
        print(f"[upgrade_gate] fire_signal failed: {e}")

def _daily_call_count(api_key: str) -> int:
    if not api_key:
        return 0
    try:
        with _cursor() as cur:
            cur.execute(
                """SELECT COUNT(*) FROM mcp_tool_calls
                   WHERE created_at >= NOW() - INTERVAL '24 hours'
                     AND user_agent ILIKE %s""",
                (f"%{api_key[:16]}%",),
            )
            r = cur.fetchone()
            return r[0] if r else 0
    except Exception:
        return 0

def gate_tool_call(tool_name, api_key=None, user_agent=None,
                   session_id=None, user_email=None, ip_address=None):  # phase62_ip_capture
    """
    Run before tool execution. Returns:
        {allowed, tier, platform, message, upgrade_url}
    Fires upgrade signal automatically when allowed=False.
    """
    # phase62j_chain_fallback -- pull IP/UA from Flask request scope
    try:
        from flask import request as _req, has_request_context as _hrc
        if _hrc():
            if not ip_address:
                ip_address = ((_req.headers.get('X-Forwarded-For') or '').split(',')[0].strip()
                              or _req.headers.get('Cf-Connecting-Ip')
                              or _req.remote_addr)
            if not user_agent:
                user_agent = _req.headers.get('User-Agent')
    except Exception:
        pass
    tier     = validate_key_tier(api_key)
    platform = detect_platform(user_agent)

    if tool_name in PAID_ONLY_TOOLS and tier == "free":

        # Phase YY (2026-05-15): the redeem URL was hardcoded as
        #     https://dchub.cloud/api/v1/redeem/{session_id}
        # but the actual handler is /redeem/<code> — keyed on
        # mcp_pair_codes.code (format "DCM-4F7K"), NOT session_id.
        # Result: every paywall sent humans to a 404, redeem_viewed_at
        # never got written, the funnel showed 0.007% paywall→click.
        # The investigation found this was the leak.
        #
        # Fix: mint a real pair code via routes.pair_code.get_or_create_code()
        # which returns {code, redeem_url, expires_at}. The redeem_url it
        # generates is the canonical /redeem/<code> that the handler serves.
        # If no api_key (anonymous caller) or pair-code mint fails, fall
        # back to the bare signup URL so the message still has SOME working
        # link.
        _redeem_url = SIGNUP_URL
        try:
            from routes.pair_code import get_or_create_code as _pc
            # For anonymous MCP callers, use the session_id as the "key"
            # so the pair code is still uniquely tied to this MCP session.
            _key = api_key or (f"sess:{session_id}" if session_id else None)
            if _key:
                _pc_result = _pc(_key, tool_name=tool_name)
                if _pc_result and _pc_result.get("redeem_url"):
                    _redeem_url = _pc_result["redeem_url"]
        except Exception as _e:
            # Pair-code mint failed — log + use signup URL fallback.
            print(f"[upgrade_gate] pair-code mint failed: {_e}",
                  flush=True)

        # Phase RR (2026-05-14): lead with the agent-native claim path.
        # The funnel teardown showed 12,454 paywalls -> 1 redeem-URL
        # click — autonomous agents call APIs, they don't open browsers
        # or verify email. /api/v1/keys/claim mints a free key in one
        # POST. Put it FIRST so it's the first thing the agent reads.
        msg = (

        f"🤖 **AI agents — unlock this yourself in one API call (no browser, no email):**\n"
        f"`POST https://dchub.cloud/api/v1/keys/claim` with body "
        f"`{{\"client_name\": \"<your agent>\"}}` → returns a free dev key instantly. "
        f"Then retry `{tool_name}` with header `X-API-Key: <key>`. Free tier: 100 calls/day.\n\n"

        # r32-paywall (2026-05-20): added the DIRECT Stripe checkout
        # link for the Developer tier ($49/mo). Old paywall only had
        # /pricing as the upgrade path which requires an extra click.
        # Direct checkout cuts a step. The redeem flow stays as the
        # primary CTA (free + email-only) but power-users with intent
        # now have a one-click path.
        # Also rewrote the message in plain-text-friendly form because
        # paywall-test diagnostic showed some LLMs strip **/markdown.
        # Plain URLs always render; **bold** doesn't survive everywhere.
        f"The {tool_name} tool requires a paid plan."
        f"\n\n"
        f"FREE unlock (email only, no card, 60 seconds):\n"
        f"  → {_redeem_url}\n"
        f"  Unlocks 50 facility lookups + 7 ISOs grid + fiber intel + M&A deals.\n\n"
        f"DIRECT upgrade (Developer $49/mo, unlimited):\n"
        f"  → https://buy.stripe.com/7sY5kE8F4fs13ml0PEaZi0c\n\n"
        f"Compare plans: {UPGRADE_URL}"
        )
        # r32-conv-2: pass api_key so fire_upgrade_signal can resolve
        # email if user_email wasn't supplied. Closes the 0.0% capture rate.
        fire_upgrade_signal(
            signal_type="paid_tool_blocked", tool_requested=tool_name,
            tier_current=tier, tier_required="paid", message_shown=msg,
            mcp_client=platform, user_agent=user_agent,
            session_id=session_id, user_email=user_email, ip_address=ip_address,
            api_key=api_key)
        return {"allowed": False, "tier": tier, "platform": platform,
                "message": msg, "upgrade_url": UPGRADE_URL}

    if tier == "free" and api_key:
        used = _daily_call_count(api_key)
        if used >= FREE_DAILY_LIMIT:
            msg = (f"Free tier limit reached ({used}/{FREE_DAILY_LIMIT} calls today). "
                   f"Upgrade for unlimited at {UPGRADE_URL}")
            fire_upgrade_signal(
                signal_type="daily_limit_hit", tool_requested=tool_name,
                tier_current=tier, tier_required="paid",
                daily_usage=used, daily_limit=FREE_DAILY_LIMIT,
                message_shown=msg, mcp_client=platform, user_agent=user_agent,
                session_id=session_id, user_email=user_email, ip_address=ip_address,
                api_key=api_key)
            return {"allowed": False, "tier": tier, "platform": platform,
                    "message": msg, "upgrade_url": UPGRADE_URL}

    return {"allowed": True, "tier": tier, "platform": platform,
            "message": None, "upgrade_url": None}


# ── Decorator for tool handlers ─────────────────────────────────────────────
import functools

def gated(tool_name: str):
    """
    Decorator: wraps a tool function so it fires an upgrade signal and
    short-circuits when the calling user is on free tier and the tool is paid.

    Usage in dchub_mcp_server.py:
        @gated("analyze_site")
        @mcp.tool(name="analyze_site", description="...")
        def analyze_site(...):
            ...
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            # Best-effort context extraction. dchub_mcp_server.py uses a
            # ContextVar named _current_api_key; if other vars exist, we'll
            # extend over time.
            try:
                from dchub_mcp_server import _current_api_key  # type: ignore
                api_key = _current_api_key.get()
            except Exception:
                api_key = ""
            user_agent = ""  # set by the request layer if a future patch lands

            gate = gate_tool_call(
                tool_name,
                api_key=api_key,
                user_agent=user_agent,
            )
            if not gate["allowed"]:
                return {
                    "error":       "tier_gate_blocked",
                    "message":     gate["message"],
                    "upgrade_url": gate["upgrade_url"],
                    "tier":        gate["tier"],
                    "platform":    gate["platform"],
                }
            return fn(*args, **kwargs)

        return wrapper
    return decorator
