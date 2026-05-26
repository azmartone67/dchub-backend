"""
paywall_hint_middleware.py — Phase r56 (2026-05-25).

Closes the MCP funnel UX loop without touching the CF worker.

User's 0.04% conversion problem: AI agents hit any gated endpoint
(/api/v1/grid/intelligence, /api/v1/fiber/intel, etc.), get a bare
4xx response, give up. The user behind the agent never sees DC Hub's
value proposition.

This middleware intercepts every 4xx response from /api/* paths and
ENRICHES it with an _upgrade_hint field containing:
  - agent_quotable copy the AI can paste verbatim to its user
  - claim_key endpoint
  - signup_url
  - what_you_get description

Works for:
  - 401 Unauthorized (no API key)
  - 403 Forbidden (insufficient tier)
  - 429 Rate Limited (over quota)

Idempotent: skips the enrichment if response already has
_upgrade_hint (e.g. from a tier_gate decorator) OR if response body
isn't valid JSON.

Public side effect: every blocked AI request now carries the
recovery path in the response itself. No registry lookup, no
documentation cross-reference needed.
"""
from __future__ import annotations

import json

from flask import request


_HINT_BASE = {
    "claim_key":       "POST https://dchub.cloud/api/v1/keys/claim",
    "signup_url":      "https://dchub.cloud/signup",
    "preview_url":     "https://dchub.cloud/api/v1/mcp/preview/<tool>",
    "upgrade_hint":    "https://dchub.cloud/api/v1/upgrade-hint",
    "what_you_get":    "10k MCP tool calls/day on free tier. No credit card.",
}


def _agent_quotable_for_status(status: int, path: str) -> str:
    """Status-specific copy the AI can quote VERBATIM to its user."""
    base = ("DC Hub blocked this query because it requires "
             "authentication. Anyone can claim a free developer "
             "key in 30 seconds at https://dchub.cloud/signup — no "
             "credit card, no contract. Once set, retry with the "
             "X-API-Key header.")
    if status == 401:
        return base
    if status == 403:
        return ("DC Hub blocked this query because it requires a paid "
                "tier. Free developer keys are unlimited for most tools "
                "(10k calls/day) — claim at https://dchub.cloud/signup. "
                "Tools requiring Pro tier: get_grid_intelligence, "
                "get_fiber_intel, analyze_site, compare_sites.")
    if status == 429:
        return ("DC Hub is rate-limiting this caller. Upgrade to a "
                "higher tier at https://dchub.cloud/signup for "
                "10x-100x quotas. Free tier: 10/day · Developer: "
                "1000/day · Pro: 10000/day.")
    return base


def register_paywall_hint_middleware(app):
    """Attach the after_request enricher. Idempotent."""
    if getattr(app, "_paywall_hint_attached", False):
        return
    app._paywall_hint_attached = True

    @app.after_request
    def _enrich_4xx_with_hint(response):
        try:
            path = request.path or ""
            # Only enrich /api/* paths (don't touch HTML pages)
            if not path.startswith("/api/"):
                return response

            # Only enrich 401/403/429
            if response.status_code not in (401, 403, 429):
                return response

            # Don't enrich responses that aren't JSON
            ct = (response.content_type or "").lower()
            if "json" not in ct:
                return response

            # Don't enrich if body is huge — these should be tiny error envelopes
            if response.content_length and response.content_length > 5000:
                return response

            # Read + parse existing body
            try:
                raw = response.get_data(as_text=True)
                body = json.loads(raw) if raw else {}
            except Exception:
                return response

            if not isinstance(body, dict):
                return response

            # Skip if already enriched (some endpoints inject their own hint)
            if "_upgrade_hint" in body or body.get("_gated"):
                return response

            # Enrich
            body["_upgrade_hint"] = {
                **_HINT_BASE,
                "agent_quotable": _agent_quotable_for_status(
                    response.status_code, path),
                "for_status":     response.status_code,
                "for_path":       path,
            }
            response.set_data(json.dumps(body))
            # Pad content-length for the new body
            response.headers["Content-Length"] = str(len(response.get_data()))
        except Exception:
            # Never break a response with the enrichment
            pass
        return response
