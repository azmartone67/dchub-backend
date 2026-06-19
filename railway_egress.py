"""Railway outbound-egress IP prefixes — single source of truth.

The backend's own self-calls (the brain hitting its own /api, the failover
warmer, sentinel probes) come from Railway's static egress IP(s). Several places
must recognize that range: the rate limiter (exempt self-calls from the anon cap,
else 429 → worker-pool starvation), the egress detector in main.py, and the
analytics/outreach/security SQL (exclude self-traffic so it isn't counted as real
visitors). Hardcoding the IPs meant every Railway IP change needed a code edit.

r-ha-static-ips (2026-06-18): Railway is migrating Legacy → HA Static IPs
(deadline 2026-07-13). The new HA IPs for resourceful-essence/dchub-backend are
152.55.176.240 (sfo), 162.220.232.250 + 162.220.232.252 (eqsv2a/cssv9a) — the
.232.x ones are covered by the existing prefix; 152.55.176. is the new range.
Prefixes are env-configurable (RAILWAY_EGRESS_PREFIXES, comma-separated) so the
NEXT IP change is a config edit, not a code change + redeploy.
"""
import os

# Default covers the CURRENT legacy IP (162.220.232.99) AND the new HA IPs, so it
# works both before and after the cutover with zero downtime.
RAILWAY_EGRESS_PREFIXES = [
    p.strip() for p in os.environ.get(
        "RAILWAY_EGRESS_PREFIXES",
        "162.220.232.,162.220.233.,152.55.176.",
    ).split(",") if p.strip()
]


def is_railway_egress(ip) -> bool:
    """True if `ip` is one of our own Railway egress IPs (a platform self-call)."""
    ip = ip or ""
    return any(ip.startswith(p) for p in RAILWAY_EGRESS_PREFIXES)
