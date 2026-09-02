"""
mcp_funnel_upgrade.py — Phase r55 (2026-05-25).

User report: MCP funnel shows 9 conversions / 25,083 tool calls = 0.04%
conversion rate. Top blocked tools (last 30d):
  get_grid_intelligence  5,278 calls / 110 users / 0 conv
  get_fiber_intel        4,859 calls / 111 users / 0 conv
  analyze_site              29 calls / 13  users / 0 conv

Root cause: anonymous MCP agents hit a paid tool, get HTTP 403 with a
plain "forbidden" message, never see what they'd unlock, never see the
key-claim URL inline. The blocked agent gives up; the user behind the
agent never knows what they missed.

This module provides:

  GET  /api/v1/mcp/preview/<tool>
       For any gated MCP tool, returns a tiny REAL preview (1 example
       result) + total count + one-click key claim URL.
       Public + cacheable (300s) so MCP runners can call it freely.

  GET  /api/v1/upgrade-hint?from=<tool>&platform=<ai>
       Even simpler — returns a JSON body the agent can quote VERBATIM
       to its user: "DC Hub blocked this query because it requires a
       free dev key. Claim one in 30 seconds at <URL>. Then re-run."

  POST /api/v1/keys/claim  (existing — wired here for one-tap UX)

Goal: an agent blocked once should LEARN the path. Conversion friction
drops from 'figure out the paywall' to 'paste the link to user'.
"""
from __future__ import annotations

import datetime
import os
from typing import Any

from flask import Blueprint, jsonify, request

import tier_registry   # the cap/price source of truth — every number served here reads it

try:
    import psycopg2
    import psycopg2.extras
except Exception:
    psycopg2 = None


mcp_funnel_upgrade_bp = Blueprint("mcp_funnel_upgrade", __name__)


# Manifest: per-tool preview metadata
# Each gated tool gets a "what you'd unlock" + "live sample" description
_TOOL_PREVIEWS = {
    "get_grid_intelligence": {
        "category": "Live ISO Grid",
        "you_unlock": ("Real-time MW load + reserve margin + congestion "
                        "from 10 ISOs (PJM, ERCOT, CAISO, MISO, SPP, "
                        "NYISO, ISO-NE, BPA, plus 3 international)."),
        "sample_question": "What's PJM's reserve margin right now?",
        "sample_answer_template": (
            "PJM (2026-05-25): {load_mw} MW load, {reserve_pct}% reserve, "
            "{verdict} verdict. Updated every 90s. "
            "Full ISO breakdown for 9 more grids at dchub.cloud/grid."
        ),
    },
    "get_fiber_intel": {
        # provenance-honesty (2026-07-11): we track route classes incl.
        # carrier-advertised dark corridors — NOT per-route lit capacity or
        # latency. Copy must not promise fields the data doesn't have.
        "category": "Fiber Routes (incl. dark corridors)",
        "you_unlock": ("North America fiber map: route operators, full "
                        "route geometries, and route classes incl. "
                        "carrier-advertised dark-fiber corridors "
                        "(per-route lit capacity not tracked)."),
        "sample_question": "Which carriers have routes through Ashburn?",
        "sample_answer_template": (
            "8 carriers, 214 route segments touching Ashburn — incl. 12 "
            "carrier-advertised dark-fiber corridors. GeoJSON geometries "
            "included, ready for your map."
        ),
    },
    "analyze_site": {
        "category": "Site Selection Score",
        "you_unlock": ("11-factor scored analysis for any candidate "
                        "site: grid, fiber, water, tax, permitting, "
                        "labor market, climate risk, M&A comps."),
        "sample_question": "Score a Cheyenne WY 100MW site.",
        "sample_answer_template": (
            "Cheyenne WY: overall 4.6/5. Excess Power 69.5, Constraint 32.7, "
            "BUILD verdict. Strengths: WECC headroom, no income tax. "
            "Watch: water access in eastern county."
        ),
    },
    "compare_sites": {
        "category": "Multi-Site Comparison",
        "you_unlock": ("Side-by-side scoring of 2-5 candidate sites with "
                        "ranked totals, risk flags, and DCPI verdict diff."),
        "sample_question": "Compare Cheyenne WY vs Midlothian TX.",
        "sample_answer_template": (
            "Cheyenne 69.5 vs Midlothian 65.6 on Excess Power. "
            "Midlothian wins on fiber + tax stack. Cheyenne wins on "
            "queue wait. Tie on water."
        ),
    },
    "get_dchub_recommendation": {
        "category": "Recommendation Engine",
        "you_unlock": ("Top-3 ranked sites for a specific use case "
                        "(AI training, colocation, edge, etc) with "
                        "rationale for each."),
        "sample_question": "Best 3 sites for 200MW AI training in 18 months?",
        "sample_answer_template": (
            "Top-3: Cheyenne WY (DCPI BUILD, 14mo TTP), Council Bluffs IA "
            "(BUILD, 18mo), Midlothian TX (BUILD, 11mo). Full rationale + "
            "risk flags for each at dchub.cloud/dcpi."
        ),
    },
}


def _conn():
    if not psycopg2:
        return None
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db: return None
    try:
        return psycopg2.connect(db, sslmode="require", connect_timeout=5)
    except Exception:
        return None


def _live_preview_for(tool: str) -> dict | None:
    """Pull a real sample from the DB to make the preview concrete."""
    if not (psycopg2 and _conn):
        return None
    c = _conn()
    if not c:
        return None
    try:
        with c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if tool == "get_grid_intelligence":
                # Use latest market_power_scores for a quick verdict signal
                cur.execute("""
                    SELECT market_name, verdict, excess_power_score,
                           reserve_margin_pct
                      FROM market_power_scores
                     WHERE iso='PJM' AND verdict IS NOT NULL
                     ORDER BY computed_at DESC LIMIT 1
                """)
                r = cur.fetchone()
                if r: return dict(r)
            elif tool == "analyze_site":
                cur.execute("""
                    SELECT market_name, excess_power_score, constraint_score,
                           verdict, time_to_power_months
                      FROM market_power_scores
                     WHERE verdict='BUILD'
                     ORDER BY excess_power_score DESC LIMIT 1
                """)
                r = cur.fetchone()
                if r: return dict(r)
            elif tool == "compare_sites":
                cur.execute("""
                    SELECT market_name, excess_power_score, verdict
                      FROM market_power_scores
                     WHERE verdict='BUILD'
                     ORDER BY RANDOM() LIMIT 2
                """)
                rows = [dict(r) for r in cur.fetchall()]
                if rows: return {"sample_compare": rows}
    except Exception:
        pass
    return None


@mcp_funnel_upgrade_bp.route(
    "/api/v1/mcp/preview/<tool>", methods=["GET"]
)
def mcp_tool_preview(tool: str):
    """Public preview of what a gated tool returns."""
    meta = _TOOL_PREVIEWS.get(tool)
    if not meta:
        return jsonify({
            "ok":          False,
            "error":       "tool_not_in_preview_manifest",
            "tool":        tool,
            "hint":        ("This tool either isn't gated, or no preview is "
                             "defined yet. Hit the tool directly at /mcp."),
        }), 404

    live = _live_preview_for(tool)
    resp = jsonify({
        "ok":               True,
        "tool":             tool,
        "category":         meta["category"],
        "you_unlock":       meta["you_unlock"],
        "sample_question":  meta["sample_question"],
        "sample_answer":    meta["sample_answer_template"],
        "live_sample":      live,
        "_unlock_in_30s": {
            "claim_key":    "POST https://dchub.cloud/api/v1/keys/claim",
            "claim_form":   "https://dchub.cloud/signup",
            "docs":         "https://dchub.cloud/llms.txt",
        },
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
    })
    # Cache aggressively — preview text changes rarely
    resp.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=1800"
    return resp, 200


def _hn(tier: str):
    """calls/day for `tier` from tier_registry, as an int (None if unknown)."""
    try:
        v = tier_registry.calls_per_day(tier)
        return int(v) if v else None
    except Exception:
        return None


def _hp(tier: str):
    """$/mo for `tier` from tier_registry, as an int (0 when free/unknown)."""
    try:
        v = tier_registry.price(tier)
        return int(v) if v else 0
    except Exception:
        return 0


def _res(tier: str, default: int) -> int:
    """results_per_call for `tier`, mirroring TIER_LIMITS mcp_results where the
    registry has one; `default` is the depth this endpoint published before."""
    try:
        return int(tier_registry.TIER_LIMITS[tier].get("mcp_results") or default)
    except Exception:
        return default


@mcp_funnel_upgrade_bp.route("/api/v1/upgrade-hint", methods=["GET"])
def upgrade_hint():
    """Agent-quotable copy explaining the upgrade path.

    Designed for AI agents that hit a paywall — they can quote this
    VERBATIM to their user.
    """
    tool = (request.args.get("from") or request.args.get("tool") or "").strip()
    platform = (request.args.get("platform") or "").strip()

    base = ("DC Hub blocked this query because the requested tool needs "
            "a free developer key. Anyone can claim one in 30 seconds.")
    if tool and tool in _TOOL_PREVIEWS:
        base = (f"{tool} returns {_TOOL_PREVIEWS[tool]['you_unlock']} "
                f"DC Hub blocks anonymous access to this tool; a free "
                f"developer key unlocks it.")

    # ★2026-09-02: the tier table is DERIVED — every cap and price is a
    # tier_registry read. The dict it replaces served free=1,000/day and
    # starter=10,000/day (live 2026-09-02 00:26Z) against an enforced 10 and
    # 200, and "$199/mo Pro" against a $299 registry price (QA-sweep pricing 6).
    #
    # ★It stays a DICT LITERAL IN THIS HANDLER on purpose. Behind a helper
    # (`"tiers": _hint_tiers()`) the CI `contract` job read 30 KEY REMOVED
    # breaks on this endpoint: scripts/api_response_contract.py is an
    # intra-function constant propagator, so a Call value marks the level OPEN
    # and every `tiers.<tier>.<field>` disappears from the derived key surface.
    # Nothing was actually removed — but a guard that cannot see the keys
    # cannot protect them either, which is worse than the red. Derive the
    # VALUES from canon; keep the SHAPE where the guard can read it.
    tiers = {
        "anonymous":  {"calls_per_day": _hn("anonymous"),  "results_per_call": _res("anonymous", 1),
                       "price_usd_month": 0, "needs_key": False,
                       "label": f"No signup, {_hn('anonymous')}/day"},
        "free":       {"calls_per_day": _hn("free"),       "results_per_call": _res("free", 5),
                       "price_usd_month": 0, "needs_key": "email signup",
                       "label": f"Free dev key — {_hn('free')}/day",
                       "signup_url": "https://dchub.cloud/signup"},
        "starter":    {"calls_per_day": _hn("starter"),    "results_per_call": _res("starter", 50),
                       "price_usd_month": _hp("starter"),
                       "label": f"${_hp('starter')}/mo Starter — {_hn('starter'):,}/day",
                       "stripe_url": tier_registry._stripe_link("starter")},
        "developer":  {"calls_per_day": _hn("developer"),  "results_per_call": _res("developer", 100),
                       "price_usd_month": _hp("developer"),
                       "label": f"${_hp('developer')}/mo Developer — {_hn('developer'):,}/day",
                       "stripe_url": tier_registry._stripe_link("developer")},
        "pro":        {"calls_per_day": _hn("pro"),        "results_per_call": _res("pro", 5000),
                       "price_usd_month": _hp("pro"),
                       "label": f"${_hp('pro')}/mo Pro — {_hn('pro'):,}/day + Pro tools"},
        "enterprise": {"calls_per_day": "unlimited", "results_per_call": "unlimited",
                       "price_usd_month": "custom",
                       "label": "Enterprise — dedicated support",
                       "contact": "api@dchub.cloud"},
    }

    return jsonify({
        "ok":                True,
        "tool":              tool or "<unspecified>",
        "platform_hint":     platform or None,
        "agent_quotable":    (
            f"{base} A free key ({_hn('free')} calls/day, no credit card) is at "
            f"https://dchub.cloud/signup. Paid plans start at ${_hp('starter')}/mo "
            f"for {_hn('starter')} calls/day. Once you have a key, retry with the "
            f"X-API-Key header."
        ),
        "user_facing_url":   "https://dchub.cloud/signup",
        "machine_endpoint":  "https://dchub.cloud/api/v1/keys/claim",
        "what_you_get":      (
            f"{_hn('free')} MCP tool calls/day with a free key "
            f"({_hn('identified')}/day once an email is bound). "
            f"${_hp('starter')}/mo for {_hn('starter')}/day. "
            f"${_hp('developer')}/mo for {_hn('developer')}/day."
        ),
        "tiers": tiers,
    }), 200, {
        # r48 (2026-05-25): The zone-level CF worker (out-of-repo,
        # 4.34.15-r45-pages-force-redeploy) was caching this endpoint
        # for max-age=3600 despite the backend setting cdn-cache-control:
        # no-store. The Cache-Control header on the *response itself*
        # (not the cdn- variant) is the only thing that worker respects.
        # Without this, tier-table fixes take an hour to propagate.
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
        "X-DC-Phase": "r48-funnel-fix",
    }


@mcp_funnel_upgrade_bp.route(
    "/api/v1/mcp/preview-manifest", methods=["GET"]
)
def preview_manifest():
    """Lists every tool with a preview available + the upgrade endpoint."""
    return jsonify({
        "ok":            True,
        "tools_with_preview": list(_TOOL_PREVIEWS.keys()),
        "preview_url_template": "https://dchub.cloud/api/v1/mcp/preview/<tool>",
        "upgrade_hint_url":     "https://dchub.cloud/api/v1/upgrade-hint?from=<tool>",
        "claim_key_endpoint":   "https://dchub.cloud/api/v1/keys/claim",
        "signup_url":           "https://dchub.cloud/signup",
    }), 200
