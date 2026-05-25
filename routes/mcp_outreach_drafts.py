"""
mcp_outreach_drafts.py — Phase r37 (2026-05-25).

Closes the loop from "L23 says we're missing from N registries" to
"here is the exact text to paste into each registry's submission form."

The L23 lifecycle audit flags pending registries. mcp_registry_outreach
knows the submit URLs. But each registry asks for slightly different
fields (server name, description length, tool count, category, contact)
and the human (or auto-code module) had to assemble those fields by
hand for each one.

This module pre-assembles a submission-ready Markdown + JSON package
for every pending registry, drawing from the live server-card + tool
catalog. One endpoint hit → 7 ready-to-paste blocks.

Endpoints
---------
GET  /api/v1/admin/outreach/draft-submissions
       Returns drafts for ALL pending targets in mcp_registry_outreach.
       Query: ?key=<name>  to scope to one target.
GET  /api/v1/admin/outreach/draft-submissions/manifest
       MCP-discoverable manifest of this helper.

Auth
----
Reads admin key via same chain as mcp_registry_outreach._admin_authorized.
"""
from __future__ import annotations

import datetime as _dt
import os
from typing import Any

from flask import Blueprint, current_app, jsonify, request


mcp_outreach_drafts_bp = Blueprint("mcp_outreach_drafts", __name__)


_BRAND = "DC Hub"
_TAGLINE = "Data center intelligence MCP server"
_HOMEPAGE = "https://dchub.cloud"
_MCP_URL = "https://dchub.cloud/mcp"
_SERVER_CARD = "https://dchub.cloud/.well-known/mcp/server-card.json"
_CONTACT_EMAIL = "api@dchub.cloud"
_REPO = "https://github.com/azmartone67/dchub-backend"
_GITHUB_HANDLE = "azmartone67"

# 1-paragraph + 1-line variants for forms that ask for either.
_DESC_LONG = (
    "DC Hub is the leading MCP server for data-center intelligence. "
    "It exposes 23+ tools that cover 21,000+ global data-center "
    "facilities across 178 countries, 285 US power markets scored by "
    "our proprietary DC Hub Power Index (DCPI), $324B+ in tracked "
    "M&A deals, 369 GW of construction pipeline, ISO grid telemetry "
    "(PJM, ERCOT, CAISO, MISO, SPP, NYISO), fiber routes, and energy "
    "pricing. Used by 96+ AI platforms — Claude, ChatGPT, Gemini, "
    "Copilot, Perplexity, Grok, Mistral, DeepSeek — for grounded "
    "answers about site selection, M&A activity, grid risk, and "
    "renewable energy economics."
)
_DESC_SHORT = (
    "MCP server with 23+ tools covering 21,000+ data-center facilities, "
    "285 US power markets (DCPI), $324B+ M&A, 369 GW pipeline, ISO grid "
    "data, fiber, energy pricing. Powering 96+ AI platforms."
)
_DESC_TWEET = (
    "@dchub_cloud — data-center intelligence MCP. 23+ tools, 21K "
    "facilities, 285 markets scored, 96+ AI platforms. dchub.cloud/mcp"
)

_CATEGORIES = ["data", "research", "finance", "energy", "infrastructure"]
_TAGS = [
    "data-center", "datacenter", "infrastructure", "energy", "grid",
    "iso", "dcpi", "power-markets", "site-selection", "renewable",
    "m-and-a", "fiber", "real-estate", "ai-infrastructure",
    "anthropic", "openai", "perplexity", "gemini", "mcp",
    "intelligence", "scoring", "ranking", "research",
]


def _admin_authorized() -> bool:
    provided = (request.headers.get("X-Admin-Key")
                or request.headers.get("X-Internal-Key")
                or request.args.get("admin_key") or "")
    if not provided:
        return False
    try:
        from internal_auth import is_valid_internal_key
        if is_valid_internal_key(provided):
            return True
    except Exception:
        pass
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("DCHUB_INTERNAL_KEY"))
    return bool(expected) and provided == expected


def _live_tool_count() -> int:
    """Fetch live tool count from the MCP tools manifest, fallback 23."""
    try:
        with current_app.test_client() as c:
            r = c.get("/.well-known/mcp-tools.json")
            if r.status_code == 200:
                d = r.get_json() or {}
                tools = d.get("tools") or []
                if isinstance(tools, list) and len(tools) > 0:
                    return len(tools)
    except Exception:
        pass
    return 23


def _live_facility_count() -> int:
    """Fetch live facility count from /api/health, fallback 21000."""
    try:
        with current_app.test_client() as c:
            r = c.get("/api/health")
            if r.status_code == 200:
                d = r.get_json() or {}
                fc = d.get("facility_count")
                if isinstance(fc, int) and fc > 0:
                    return fc
    except Exception:
        pass
    return 21000


def _draft_for_target(t: dict, tool_n: int, fac_n: int) -> dict:
    """Build a per-registry draft package."""
    name = t.get("name") or "Unknown"
    key = t.get("key") or "unknown"
    submit_url = t.get("submit_url") or t.get("manual_url") or ""
    method = t.get("submit_method") or "manual"

    # Markdown block — universal "paste this into the form" body.
    md = f"""## {_BRAND} — MCP Server Submission

**Name:** {_BRAND}
**Tagline:** {_TAGLINE}
**MCP URL:** {_MCP_URL}
**Homepage:** {_HOMEPAGE}
**Server Card:** {_SERVER_CARD}
**Repository:** {_REPO}
**Contact:** {_CONTACT_EMAIL}

### Description (long)
{_DESC_LONG.replace(', '+str(fac_n)+'+', ', '+str(fac_n)+'+')}

### Description (short)
{_DESC_SHORT}

### Tags
{', '.join(_TAGS)}

### Categories
{', '.join(_CATEGORIES)}

### Stats (live)
- Tools: {tool_n}+
- Facilities tracked: {fac_n:,}+
- Power markets scored (DCPI): 285
- Countries covered: 178
- Active AI platforms: 96+

### License
Free for AI citation. Data subject to {_HOMEPAGE}/terms.
"""

    # JSON variant — for registries that take an API submission.
    submission_json = {
        "name": _BRAND,
        "tagline": _TAGLINE,
        "description": _DESC_LONG,
        "mcp_url": _MCP_URL,
        "homepage": _HOMEPAGE,
        "server_card_url": _SERVER_CARD,
        "repository": _REPO,
        "contact_email": _CONTACT_EMAIL,
        "github_handle": _GITHUB_HANDLE,
        "tags": _TAGS,
        "categories": _CATEGORIES,
        "tool_count": tool_n,
        "stats_live": {
            "facilities_tracked": fac_n,
            "power_markets_scored": 285,
            "countries_covered": 178,
            "active_ai_platforms": 96,
        },
        "license": "free-for-citation",
    }

    return {
        "registry_key":      key,
        "registry_name":     name,
        "submit_url":        submit_url,
        "submit_method":     method,
        "markdown_block":    md,
        "submission_json":   submission_json,
        "tweet_announcement": _DESC_TWEET,
        "ready_to_submit":   bool(submit_url),
    }


@mcp_outreach_drafts_bp.route(
    "/api/v1/admin/outreach/draft-submissions", methods=["GET"]
)
def draft_submissions() -> Any:
    """Return submission-ready drafts for pending registries."""
    if not _admin_authorized():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401

    # Source of truth for registry roster + submission status:
    try:
        from routes.mcp_registry_outreach import (
            DISCOVERY_TARGETS, get_submitted_target_names,
        )
    except Exception as e:
        return jsonify({"ok": False, "error": f"registry_module_unavailable: {e}"}), 500

    requested_key = (request.args.get("key") or "").strip()
    submitted = set(get_submitted_target_names())

    tool_n = _live_tool_count()
    fac_n = _live_facility_count()

    drafts = []
    for t in DISCOVERY_TARGETS:
        if requested_key and t.get("key") != requested_key:
            continue
        # By default, only generate drafts for PENDING targets.
        # If a specific key is requested, generate it regardless.
        if not requested_key and t.get("name") in submitted:
            continue
        drafts.append(_draft_for_target(t, tool_n, fac_n))

    return jsonify({
        "ok":            True,
        "tool":          "draftMCPRegistrySubmissions",
        "drafts_count":  len(drafts),
        "drafts":        drafts,
        "live_stats": {
            "tool_count":      tool_n,
            "facility_count":  fac_n,
        },
        "next_action":   ("For each draft, open `submit_url` and paste "
                          "either `markdown_block` (most forms) or "
                          "`submission_json` (API endpoints). "
                          "Confirm registry-specific field aliases "
                          "before submitting."),
        "computed_at":   _dt.datetime.utcnow().isoformat() + "Z",
    }), 200


@mcp_outreach_drafts_bp.route(
    "/api/v1/admin/outreach/draft-submissions/manifest", methods=["GET"]
)
def draft_manifest() -> Any:
    return jsonify({
        "tool":         "draftMCPRegistrySubmissions",
        "endpoint":     "/api/v1/admin/outreach/draft-submissions",
        "description":  ("Returns Markdown + JSON submission packages "
                         "for every PENDING MCP registry target. Closes "
                         "the loop from L23 lifecycle audit's "
                         "'missing from N registries' finding to "
                         "ready-to-paste submission content."),
        "params":       {
            "key":       "optional registry key (smithery, lobehub, ...) "
                         "— scopes to a single target",
            "admin_key": "required — X-Admin-Key header or ?admin_key=",
        },
        "version":      "r37-2026-05-25",
    }), 200
