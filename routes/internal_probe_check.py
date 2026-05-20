"""
Phase FF+25-followup (2026-05-20) — internal-probe acknowledgment.
==========================================================================

A tiny endpoint that internal probes can hit to verify the backend
recognizes them as internal (skipping rate limits + future bypass logic).

GET /api/v1/internal/probe-ack

Returns:
  200 + {"internal": true,  "ua": "..."}    when UA matches DCHub pattern
  200 + {"internal": false, "ua": "..."}    when it doesn't

NOT a security gate — only confirms the UA detection works. No auth
required. The actual rate-limit bypass lives in main.py:enforce_tier_rate_limits.

Useful for:
  - QA probes checking they're not being rate-limited
  - Diagnostics when probes start failing (curl this from various
    User-Agents to see what gets recognized)
  - Self-healing brain detecting that the bypass code is deployed
"""
from flask import Blueprint, jsonify, request

internal_probe_check_bp = Blueprint("internal_probe_check", __name__)


def is_internal_probe_ua(ua: str) -> bool:
    """Return True if User-Agent matches our internal probe pattern.
    Mirrors the check in main.py:enforce_tier_rate_limits.
    Update both together if either changes."""
    if not ua:
        return False
    lower = ua.lower()
    return (lower.startswith('dchub') or
            ua.startswith('DCHub') or
            'dchub-' in lower or
            'dchub/' in lower)


@internal_probe_check_bp.route("/api/v1/internal/probe-ack",
                                methods=["GET", "HEAD"])
def probe_ack():
    """Confirm internal-probe detection status. No auth required."""
    ua = request.headers.get('User-Agent', '')
    internal = is_internal_probe_ua(ua)
    return jsonify(
        ok=True,
        internal=internal,
        ua=ua[:120],
        message=("Recognized as internal probe — rate-limit bypassed"
                  if internal else
                  "NOT recognized as internal — full rate-limit applies. "
                  "Set User-Agent to 'DCHub-<your-probe-name>/1.0' to bypass."),
    )
