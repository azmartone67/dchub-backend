"""Declared partner egress addresses — single source of truth.

A hosted MCP catalogue proxies every one of its customers through its own
egress, so anything keyed on source IP sees one caller where there are many.
For a partner that has DECLARED its egress, these places treat that address as
the partner rather than as one visitor:

  flask_mcp_endpoints._partner_meter_scope   key-claim allowance per workspace
                                             (needs a matching client_name too)
  rate_limiter._get_key_and_tier             keyless requests get the 'partner'
  main._get_request_tier                     rate tier instead of the per-IP
                                             anonymous bucket
  routes.api_usage_tracker                   a daily count of those keyless
                                             requests, by route and status
  utils.paywall_response                     pay links served to them carry a
                                             ref recorded to the partner

The rate tier lifts the REQUEST RATE only. Paywall and tier gates, record caps
and field gating resolve the caller separately and never read this registry.

AnythingMCP: egress a single /32 declared by Matteo Morelli 2026-09-21, who has
undertaken to warn before it changes. An unannounced new egress is treated as
"not them" — every consumer falls back to its per-IP behaviour rather than
failing open.

Env override DCHUB_PARTNER_EGRESS (JSON {prefix: [ip, ...]}) so a declared host
move is a config change, not a deploy. It is read on every call, never cached.
"""
import json
import os

PARTNER_EGRESS_DEFAULT = {
    "anythingmcp/": ("104.248.242.235",),
}


def partner_egress():
    """{partner prefix: (declared egress ip, ...)} — the env override if set."""
    raw = os.environ.get("DCHUB_PARTNER_EGRESS", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return {str(k): tuple(str(i) for i in (v or ()))
                        for k, v in parsed.items()}
        except Exception:
            pass  # a malformed override must not widen anything
    return PARTNER_EGRESS_DEFAULT


def partner_for_ip(ip):
    """The partner prefix whose declared egress is exactly `ip`, else None.

    Exact string match: a neighbouring address in the same range is not the
    partner.
    """
    ip = (ip or "").strip()
    if not ip:
        return None
    for prefix, ips in partner_egress().items():
        if ip in ips:
            return prefix
    return None


CLAIM_KEY_URL = "https://dchub.cloud/api/v1/keys/claim"
CONNECT_URL = "https://dchub.cloud/connect"


def shared_allowance_429(per_minute, per_hour):
    """What a 429 adds when the exhausted bucket is a partner's SHARED keyless one.

    Both limiters give the declared egress one bucket for every keyless customer
    of the partner, so "retry later" alone leaves a caller no way out. A key is
    bucketed per key in both limiters, so a free key does get its own limit.

    The partner relays our error bodies verbatim to its customers' agents, so
    this states facts only: no instruction to the reader, no price. The numbers
    are the caller's own limiter row, passed in, never typed here.
    """
    return {
        "shared_allowance": (
            "This limit ({:,} requests per minute, {:,} per hour) is shared by "
            "every request without an API key that reaches DC Hub through this "
            "platform. A request with an API key is counted against that key's "
            "own limit instead. A free key is one POST to {} (no email, no "
            "account); {} lists the ways to connect.".format(
                int(per_minute), int(per_hour), CLAIM_KEY_URL, CONNECT_URL)),
        "free_key_url": CLAIM_KEY_URL,
        "connect_url": CONNECT_URL,
    }


def partner_rate_limited(tier, window, limit, per_minute, per_hour, retry_after):
    """main.py's 429 body for the partner tier: its usual fields, minus the
    "Upgrade your plan" ask and the bare /pricing link, plus the fact above.

    `window` is 'minute' or 'hour', the axis that was exhausted.
    """
    if window == "hour":
        body = {
            "success": False,
            "error": "rate_limited",
            "message": f"Hourly limit exceeded ({limit}/hr for {tier} tier).",
            "tier": tier,
            "limit_per_hour": limit,
            "retry_after_seconds": retry_after,
        }
    else:
        body = {
            "success": False,
            "error": "rate_limited",
            "message": f"Rate limit exceeded ({limit}/min for {tier} tier).",
            "tier": tier,
            "limit_per_minute": limit,
            "retry_after_seconds": retry_after,
        }
    body.update(shared_allowance_429(per_minute, per_hour))
    return body
