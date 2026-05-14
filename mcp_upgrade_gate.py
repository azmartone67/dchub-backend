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
                        session_id=None, user_email=None, ip_address=None):  # phase62_ip_capture
    """Insert a row into mcp_upgrade_signals. Never raises — telemetry is fire-and-forget."""
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

        # phase63_redeem_msg -- include per-session redeem URL

        _redeem_url = f"https://dchub.cloud/api/v1/redeem/{session_id}?source=mcp&tool={tool_name}&tier=developer" if session_id else SIGNUP_URL

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

        f"🔓 **The {tool_name} tool requires a paid plan.**"

        f"\n\n👉 **Human handoff:** get a free dev key here:\n{_redeem_url}\n\nNo credit card. Unlocks 50 facility lookups, real-time grid for 7 ISOs, fiber intel, M&A deals.\n\n"

        f"_Or upgrade to Pro at {UPGRADE_URL} for $49/mo unlimited access._"

        )
        fire_upgrade_signal(
            signal_type="paid_tool_blocked", tool_requested=tool_name,
            tier_current=tier, tier_required="paid", message_shown=msg,
            mcp_client=platform, user_agent=user_agent,
            session_id=session_id, user_email=user_email, ip_address=ip_address)
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
                session_id=session_id, user_email=user_email, ip_address=ip_address)
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
