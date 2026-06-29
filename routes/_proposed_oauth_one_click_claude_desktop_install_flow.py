"""
oauth-one-click-claude-desktop-install-flow.py — STRATEGIC SCAFFOLD (auto-drafted by Brain L6, week 2026-06-29).

This file is a SCAFFOLD only. The brain wrote it to start the conversation
on a feature it thinks DC Hub should ship next. It does not implement the
feature — a human reviews the spec below and turns this into a real route.

SPEC (from brain L6 synthesis):
Smithery and Glama both ship 'one-click install in Claude Desktop' and an OAuth flow tester as baseline features; lobehub adds one-click install too. DC Hub is listed on all major registries with 51 tools but has no comparable frictionless install — every new agent caller must hand-configure. The smallest shippable version: generate a copy-paste mcp.json install block plus a deep-link install button on /api-docs and the MCP listing, scoped to our existing 51-tool manifest. This directly attacks the activation gap behind the real-caller drought (only ~200 distinct paid users despite millions of requests). Success = measurable increase in new identified keys originating from install-link referrals. Measure via referer attribution on first-call for new keys.

Evidence cited by the brain when proposing this:
- `competitor.presence.competitor_features`
- `funnel.now.paid_tool_demand_30d`

To unblock: implement the routes and remove the NotImplementedError below.
"""
from flask import Blueprint, jsonify

strategic_oauth_one_click_claude_desktop_install_flow_bp = Blueprint("oauth-one-click-claude-desktop-install-flow", __name__)


@strategic_oauth_one_click_claude_desktop_install_flow_bp.route("/api/v1/strategic-scaffold/oauth-one-click-claude-desktop-install-flow", methods=["GET"])
def _scaffold_health():
    """Health probe so the blueprint can register without 500ing.
    Real endpoint TBD by the human reviewer."""
    return jsonify(
        ok=False,
        scaffold=True,
        message=("This is a strategic scaffold drafted by Brain L6. "
                  "It is not implemented yet."),
        spec_doc="docs/strategic/oauth-one-click-claude-desktop-install-flow.md",
    ), 501
