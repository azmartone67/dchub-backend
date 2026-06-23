"""
one-click-install-in-claude-desktop.py — STRATEGIC SCAFFOLD (auto-drafted by Brain L6, week 2026-06-15).

This file is a SCAFFOLD only. The brain wrote it to start the conversation
on a feature it thinks DC Hub should ship next. It does not implement the
feature — a human reviews the spec below and turns this into a real route.

SPEC (from brain L6 synthesis):
Smithery and lobehub both ship 'one-click install in Claude Desktop' as a baseline feature, while DC Hub's /connect/claude-desktop page is a healthy-but-orphan static install guide (18KB, verdict='orphan', not brain-tracked). With Claude driving 101,949 of our requests and our presence live in 15 registries at 48 tools, friction at the install step leaks our single largest external platform. Ship the smallest version: a deep-link button on /connect/claude-desktop that emits a pre-filled mcp config / .dxt manifest (we already have a dxt.so listing) so a user adds DC Hub in one action rather than copy-pasting JSON. Success = measurable install-link clicks on the connect pages, which today emit zero tracked events. This closes a concrete feature parity gap against the two registries with the strongest curation.

Evidence cited by the brain when proposing this:
- `competitor_signal.presence.competitor_features`
- `page_health.pages`

To unblock: implement the routes and remove the NotImplementedError below.
"""
from flask import Blueprint, jsonify

strategic_one_click_install_in_claude_desktop_bp = Blueprint("one-click-install-in-claude-desktop", __name__)


@strategic_one_click_install_in_claude_desktop_bp.route("/api/v1/strategic-scaffold/one-click-install-in-claude-desktop", methods=["GET"])
def _scaffold_health():
    """Health probe so the blueprint can register without 500ing.
    Real endpoint TBD by the human reviewer."""
    return jsonify(
        ok=False,
        scaffold=True,
        message=("This is a strategic scaffold drafted by Brain L6. "
                  "It is not implemented yet."),
        spec_doc="docs/strategic/one-click-install-in-claude-desktop.md",
    ), 501
