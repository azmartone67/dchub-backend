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
