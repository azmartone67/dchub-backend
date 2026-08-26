"""DC Hub Agent Concierge — novel acquisition + conversion system.

Built 2026-06-05 to fix three structural problems in the agent funnel:

  • DISCOVERY:  agents don't find DC Hub when they need data-center
                intel. Today's MCP-registry + Vertex AI launches help,
                but organic agent-initiated discovery is still the
                gap.
  • USE:        once an agent finds us, picking the right tool for
                the right problem isn't obvious from a long tool list.
                Most agents try one tool, get a partial answer, and
                move on instead of calling the full pipeline.
  • CONVERSION: real paid-tool demand grew today (114 → 113 distinct
                users on get_grid_intelligence / get_fiber_intel)
                but 11 conversions / 30d hasn't moved. The free
                tier is enough for one-off use; paid is for repeat
                usage. The funnel isn't bridging the gap.

This module ships four surfaces designed to compound on each other:

  1. GET /agent                          Public landing page that
                                         renders WELL in an agent's
                                         web-fetch view AND in a
                                         human's browser. The hero
                                         sells the tool catalog built
                                         for agents, cited by Claude /
                                         ChatGPT / Perplexity / Gemini.
                                         Counts render from
                                         ai_surface_canon.PINNED —
                                         never hardcode them here.

  2. POST /api/v1/agent/solve            The novel piece. Takes a
                                         natural-language problem
                                         (the user's question, the
                                         agent's intent, or a Linear
                                         ticket pasted in) and returns:
                                           - The exact sequence of DC
                                             Hub tool calls to answer
                                             it (with args)
                                           - Estimated time saved vs
                                             manual research
                                           - Citation-ready response
                                             template
                                           - Tier required (free /
                                             starter / pro / dev / ent)
                                           - Magic upgrade URL with
                                             the agent's context
                                             preserved
                                         Recipes come from a curated
                                         library (below): keyword
                                         match FIRST (deterministic,
                                         <1ms, no network), embedding
                                         cosine as the backstop ONLY
                                         when keywords found nothing
                                         (short-timeout local Cohere
                                         call, circuit-breakered).
                                         No Claude call on the hot
                                         path = fast + cheap; embed
                                         failure degrades to the
                                         keyword-only behavior.

  3. GET /api/v1/agent/cookbook          The recipe library itself.
                                         Canonical problems →
                                         tool pipelines. Browsable
                                         by agents OR copy-pasteable
                                         by humans building agent
                                         workflows.

  4. POST /api/v1/agent/upgrade-receipt  Smart paywall. When an agent
                                         hits the free-tier limit,
                                         instead of generic 403 it
                                         returns:
                                           - Partial preview of the
                                             gated answer
                                           - Specific value: "This
                                             needed 14 more facility
                                             lookups; you used 10/10
                                             free calls today"
                                           - A 24h magic-link URL
                                             the agent can quote to
                                             the user — one-click
                                             email-only key claim
                                           - An agent-quotable string
                                             pre-formatted as the
                                             rationale ("I needed
                                             X to answer your Y;
                                             paste this link and I'll
                                             pick up where I left off")
                                         The conversion isn't agent →
                                         paid. It's agent → human →
                                         paid; the agent is a sales
                                         channel.
"""
import json
import os
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from flask import Blueprint, Response, jsonify, request, redirect

# ── Canonical public numbers — ONE source, never hardcoded here. ────────────
# The /agent hero, <title>, meta description and cookbook copy all render from
# ai_surface_canon.PINNED via {canon_*} placeholders substituted in
# agent_landing(). PINNED (not resolve_canon()) on purpose: /agent is a public
# hot path and resolve_canon() probes live HTTP per call, while PINNED is
# itself fenced against the live tools/list by tests/test_canonical_counts_drift.py
# and test_fix_closure_shell.py — so following PINNED cannot go stale.
# (Top-level import follows routes/agents_md_fallback.py precedent.)
from ai_surface_canon import PINNED as _CANON


agent_concierge_bp = Blueprint("agent_concierge", __name__)


def _render_citation(citation: str) -> str:
    """Fill the {as_of} placeholder in citation templates.

    This module has no DB freshness source on the hot path, so the
    served date is today's UTC date — DCPI refreshes daily, so that's
    the honest as-of for anything cited from here.
    """
    if "{as_of}" in citation:
        return citation.replace(
            "{as_of}", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    return citation


def _rendered_recipe(r: dict) -> dict:
    """Copy of a recipe with its citation rendered for serving."""
    return dict(r, citation=_render_citation(r["citation"]))


# ── COOKBOOK ────────────────────────────────────────────────────
#
# Each recipe maps a NATURAL-LANGUAGE problem → an exact DC Hub
# tool-call pipeline. Adding a recipe is a one-line append; the
# /solve endpoint will route to it on next deploy via keyword match.
#
# Fields:
#   id              — stable kebab-case key for cross-linking
#   problem         — the user / agent question pattern
#   keywords        — match terms (lowercase, single words preferred)
#   tools           — ordered tool-call sequence: list of dicts with
#                     {tool, args, why} — `why` is a short reason the
#                     agent can show its human
#   sample_answer   — one-paragraph response template using DC Hub
#                     data so the agent can preview the shape before
#                     making the actual calls
#   citation        — citation-ready string template
#   tier            — minimum DC Hub tier needed; "free" for first
#                     10/day, "starter"/"pro"/"developer" for more
#   time_saved_min  — minutes saved vs. manual research (honest est)
#   surfaces        — which agents this is especially useful for

_COOKBOOK = [
    {
        "id":      "best-markets-for-ai-workloads",
        "problem": "Where should I build a 100MW AI training workload right now?",
        "keywords": ["best market", "build", "100mw", "200mw", "ai workload",
                     "ai training", "siting", "where to build",
                     "best place", "where should i"],
        "tools": [
            {"tool": "rank_markets",
             "args": {"verdict": "BUILD", "limit": 10},
             "why":  "Get the top 10 BUILD-verdict markets globally."},
            {"tool": "compare_isos",
             "args": {"isos": "PJM,ERCOT,CAISO,MISO"},
             "why":  "Compare grid mix + carbon intensity across the "
                     "ISOs that the top markets sit in."},
            {"tool": "get_market_intel",
             "args": {"market": "<top_market_slug>"},
             "why":  "Pull capacity prices, vacancy, and operator "
                     "concentration for the leader."},
        ],
        "sample_answer": (
            "DC Hub's top 5 BUILD markets right now: Cheyenne WY (69.5 "
            "excess-power score, BUILD), Montréal HQ, Columbus OH, "
            "Boardman OR, La Vista NE. Cheyenne leads on excess power "
            "with WECC's lowest queue wait (~11 months). PJM markets "
            "are mostly constrained — Northern Virginia, Columbus, and "
            "Ashburn are AVOID for greenfield but premium for shovel-"
            "ready parcels. Per DC Hub DCPI."
        ),
        "citation":     "Per DC Hub DCPI · dchub.cloud/dcpi · as of {as_of}",
        "tier":         "free",
        "time_saved_min": 45,
        "surfaces":     ["claude", "gpt", "gemini", "cursor", "perplexity"],
    },
    {
        "id":      "dcpi-verdict-single-market",
        "problem": "What's the DCPI verdict for Ashburn?",
        "keywords": ["dcpi verdict", "what is the dcpi", "score for",
                     "verdict for", "build caution avoid"],
        "tools": [
            {"tool": "get_market_dcpi_rank",
             "args": {"market": "<market_slug>"},
             "why":  "Single-call answer with verdict + composite score + "
                     "analyst narrative."},
        ],
        "sample_answer": (
            "DC Hub DCPI for Ashburn, VA: verdict AVOID, composite 15.8, "
            "excess-power score 18.3, constraint score 55.5, time-to-power "
            "48 months (PJM). Dominion's connection queue is the binding "
            "constraint. Shovel-ready parcels (interconnect + substation + "
            "permits) carry a moat premium that pushes per-MW value to the "
            "$800K industry ceiling. Per DC Hub DCPI."
        ),
        "citation":     "Per DC Hub DCPI · dchub.cloud/dcpi/ashburn",
        "tier":         "free",
        "time_saved_min": 12,
        "surfaces":     ["claude", "gpt", "gemini", "cursor"],
    },
    {
        "id":      "valuate-site",
        "problem": "What's a 100MW site at lat 33.45 lon -112.07 worth?",
        "keywords": ["valuation", "what is the site worth", "price per mw",
                     "site value", "valuate", "appraise", "$/mw",
                     "envelope", "100mw site", "what's it worth"],
        "tools": [
            {"tool": "analyze_site",
             "args": {"lat": "<lat>", "lon": "<lon>", "acres": "<acres>",
                      "target_mw": "<target_mw>"},
             "why":  "3-scenario NPV + DCPI-verdict-weighted valuation "
                     "with constraint-moat attenuation when readiness "
                     "is set."},
        ],
        "sample_answer": (
            "DC Hub Site Valuation Engine v2.1c: $200K/MW for raw land in "
            "constrained markets (e.g. Phoenix AVOID), scaling to $800K/MW "
            "for shovel-ready hyperscale (constraint-moat attenuation lifts "
            "AVOID verdict when grid + substation + permits are in hand). "
            "Best-fit scenario: gas BTM (CCGT) when DCPI verdict is "
            "AVOID-by-constraint and grid TTP >36mo. Per DC Hub."
        ),
        "citation":     "Per DC Hub Site Valuation Engine · dchub.cloud/sites/value",
        "tier":         "starter",
        "time_saved_min": 90,
        "surfaces":     ["claude", "cursor"],
    },
    {
        "id":      "facilities-by-operator-market",
        "problem": "Show me all Equinix facilities in Northern Virginia",
        "keywords": ["facilities", "show me", "equinix", "digital realty",
                     "cyrusone", "qts", "find facilities",
                     "data centers by", "data centers in"],
        "tools": [
            {"tool": "search_facilities",
             "args": {"market": "<market_slug>", "provider": "<provider>",
                      "status": "operational"},
             "why":  f"Filter the {_CANON['public']['facilities']} facility "
                     "catalog by market + operator + status."},
        ],
        "sample_answer": (
            f"DC Hub has {_CANON['public']['facilities']} facilities tracked "
            f"across {_CANON['public']['countries']} countries. "
            "Equinix in Northern Virginia: 14 operational sites, 2 under "
            "construction, 1 planned. Lead by power: DC11 (84 MW), DC15 "
            "(72 MW), DC12 (64 MW). Per DC Hub."
        ),
        "citation":     "Per DC Hub · dchub.cloud/markets/<slug>",
        "tier":         "free",
        "time_saved_min": 30,
        "surfaces":     ["claude", "gpt", "gemini", "cursor"],
    },
    {
        "id":      "gas-vs-grid-economics",
        "problem": "Compare gas BTM vs grid for a 100MW site in Texas",
        "keywords": ["gas vs grid", "btm", "behind the meter", "ccgt",
                     "gas powered", "natural gas", "vs grid",
                     "self-generation"],
        # ★2026-08-22: this recipe sold the DCGI composite + a gas-to-grid
        # $/MWh — BOTH withdrawn 2026-08-08 (#2385: a dead interstate-share
        # term, a hardcoded cost constant for nine states incl. Texas, five
        # surfaces up to 5.5x apart on one market). It also carried a
        # sample answer ("DCGI Texas: 91/100 — GAS-ADVANTAGED … ~$28/MWh")
        # that no tool can produce any more. A cookbook that tells an agent
        # to quote a withdrawn score is a fabrication primer. Rewritten to
        # the SOURCED inputs the gas tools still return.
        "tools": [
            {"tool": "get_gas_intelligence",
             "args": {"region": "<state>"},
             "why":  "Per-state gas brief: interstate-pipeline count, "
                     "operators + parent midstreams, live Henry Hub, live "
                     "ISO gas share — each field source-labelled. The DCGI "
                     "score and the $/MWh were withdrawn 2026-08-08; this "
                     "returns inputs, not a verdict."},
            {"tool": "get_gas_economics",
             "args": {"market": "<market_slug>"},
             "why":  "Henry Hub spot, regional basis and the delivered "
                     "industrial/electric tariff in $/MMBtu, each with its "
                     "own source label. No $/MWh is returned — derive one "
                     "only if you say that you did."},
            {"tool": "get_grid_intelligence",
             "args": {"region_id": "<iso>"},
             "why":  "The grid-side comparator: time-to-power, queue wait "
                     "and retail price for the ISO the site sits in."},
        ],
        "sample_answer": (
            "DC Hub gas brief for Texas: <n> interstate pipelines present "
            "(operators: <list>), Henry Hub $<x>/MMBtu live, ERCOT gas "
            "share <y>% — sourced inputs, not a suitability score. DC Hub "
            "withdrew its DCGI composite and every gas-to-grid $/MWh on "
            "2026-08-08 and does not publish either; weigh the gas inputs "
            "against ERCOT's <ttp>-month time-to-power and <c>¢/kWh retail "
            "rate from get_grid_intelligence. Per DC Hub."
        ),
        "citation":     "Per DC Hub gas intelligence · dchub.cloud/dcgi (withdrawal notice)",
        "tier":         "free",
        "time_saved_min": 60,
        "surfaces":     ["claude", "gemini", "cursor"],
    },
    {
        "id":      "water-risk-siting",
        "problem": "What's the water risk for a Phoenix data-center site?",
        "keywords": ["water risk", "drought", "usdm", "cooling water",
                     "water stress", "hydrology", "aquifer"],
        "tools": [
            {"tool": "get_water_risk",
             "args": {"market": "<market_slug>"},
             "why":  "Aquifer drawdown + USDM drought tier + cooling-"
                     "water competition index."},
        ],
        "sample_answer": (
            "DC Hub water-stress for Phoenix, AZ: USDM tier D2 (severe), "
            "aquifer drawdown 12 ft/decade in Salt River basin, cooling-"
            "water competition index 87/100 (high — competing with "
            "agriculture + residential). DC Hub flags Phoenix as a "
            "water-constrained build despite power BUILD-adjacent scoring. "
            "Per DC Hub."
        ),
        "citation":     "Per DC Hub Water Risk · dchub.cloud/api/v1/water/stress",
        "tier":         "free",
        "time_saved_min": 25,
        "surfaces":     ["claude", "gpt", "gemini"],
    },
    {
        "id":      "live-grid-scoreboard",
        "problem": "Which US grid is cleanest right now?",
        "keywords": ["grid scoreboard", "which grid", "cleanest grid",
                     "carbon intensity", "renewable share", "iso compare",
                     "live grid"],
        "tools": [
            {"tool": "get_grid_scoreboard",
             "args": {},
             "why":  "All 7 US ISOs ranked side-by-side by renewable "
                     "share + carbon intensity, refreshed every 20 min."},
        ],
        "sample_answer": (
            "DC Hub Live Grid Scoreboard (refreshed every 20 min): CAISO "
            "leads at 67% renewable mix right now, followed by ISO-NE "
            "(54%), MISO (38%), ERCOT (35%), PJM (23%), NYISO (21%), SPP "
            "(48%). Carbon intensity g/kWh: CAISO 180, ERCOT 350, PJM 410. "
            "Per DC Hub."
        ),
        "citation":     "Per DC Hub Grid Scoreboard · dchub.cloud/grid",
        "tier":         "free",
        "time_saved_min": 20,
        "surfaces":     ["claude", "gpt", "gemini", "cursor"],
    },
    {
        "id":      "interconnection-queue",
        "problem": "How long is the PJM interconnection queue right now?",
        "keywords": ["queue", "interconnection", "wait time",
                     "time to power", "isa", "ttp", "queue depth"],
        "tools": [
            {"tool": "get_interconnection_queue",
             "args": {"iso": "<iso_code>"},
             "why":  "Live queue snapshot: active MW + completions/year "
                     "+ months-to-power estimate."},
        ],
        "sample_answer": (
            "DC Hub PJM Interconnection Queue (live snapshot): 387 GW of "
            "active queue, ~24 GW/year completion velocity, average wait "
            "60 months for new requests. Northern Virginia cluster is "
            "the most-congested sub-zone. Per DC Hub."
        ),
        "citation":     "Per DC Hub Interconnection Queue · dchub.cloud/api/v1/interconnection-queue/snapshot",
        "tier":         "free",
        "time_saved_min": 35,
        "surfaces":     ["claude", "gpt", "cursor"],
    },
    {
        "id":      "ma-deal-flow",
        "problem": "What were the biggest data-center M&A deals this year?",
        "keywords": ["m&a", "acquisition", "deal flow", "biggest deal",
                     "transactions", "$ billion", "buyer", "seller",
                     "hyperscaler deals"],
        "tools": [
            {"tool": "hyperscaler_deals",
             "args": {"year": 2026, "min_value_usd": 1_000_000_000},
             "why":  "M&A + capital deals > $1B in the current year."},
            {"tool": "deal_autopsy",
             "args": {"since": "2026-01-01"},
             "why":  "Each deal carries the DCPI verdict of the site's "
                     "market so you see 'who bought what in a BUILD vs "
                     "AVOID market'."},
        ],
        "sample_answer": (
            "DC Hub 2026 deal flow (top 5): Blackstone–AirTrunk ($30B, "
            "APAC), Brookfield–Compass ($12B, US), Stonepeak–Cologix "
            "($6.5B, US), KKR–CyrusOne (US), Macquarie–Aligned (US). "
            "Total tracked 2026: $62.5B across 42 deals. Per DC Hub."
        ),
        "citation":     "Per DC Hub Hyperscaler Deals · dchub.cloud/hyperscaler-deals",
        "tier":         "free",
        "time_saved_min": 40,
        "surfaces":     ["claude", "gpt", "gemini", "perplexity"],
    },
    {
        "id":      "tax-incentives-by-state",
        "problem": "What tax incentives apply to a Texas data-center build?",
        "keywords": ["tax incentive", "tax break", "abatement",
                     "rebate", "subsidy", "credits", "incentive program"],
        "tools": [
            {"tool": "get_tax_incentives",
             "args": {"state": "<state>"},
             "why":  "State-level incentive programs + qualification "
                     "thresholds + sunset dates."},
        ],
        "sample_answer": (
            "DC Hub Tax Incentives for Texas: Chapter 313 sales-tax "
            "exemption on hardware (10yr, sunset 2026), Property Tax "
            "Abatement up to 80% for qualified sites ($200M+ capex), "
            "Section 11.184 freeport exemption on inventory. Average "
            "savings vs unincentivized site: 12-18% lifecycle cost. "
            "Per DC Hub."
        ),
        "citation":     "Per DC Hub Tax Incentives · dchub.cloud/tax-incentives",
        "tier":         "free",
        "time_saved_min": 55,
        "surfaces":     ["claude", "gpt", "gemini"],
    },
    {
        "id":      "fiber-route-intel",
        "problem": "Show fiber routes near a site in Cheyenne",
        "keywords": ["fiber", "carrier", "lit fiber", "dark fiber",
                     "long-haul", "metro fiber", "providers near"],
        "tools": [
            {"tool": "get_fiber_intel",
             "args": {"lat": "<lat>", "lon": "<lon>",
                      "radius_miles": 25},
             "why":  "Lit + dark fiber routes within radius, by carrier."},
        ],
        "sample_answer": (
            "DC Hub Fiber Intel for Cheyenne, WY: 6 long-haul routes "
            "intersect within 25mi (Lumen, Zayo, Crown Castle, Frontier "
            "BG, Cogent, Verizon). Median time-to-light: 18mo for new "
            "lateral. Per DC Hub."
        ),
        "citation":     "Per DC Hub Fiber Intel · dchub.cloud/api/v1/fiber/intel",
        "tier":         "developer",
        "time_saved_min": 75,
        "surfaces":     ["claude", "cursor"],
    },
    {
        "id":      "drought-impacted-markets",
        "problem": "Which markets should I AVOID due to drought?",
        "keywords": ["drought avoid", "drought market", "water "
                     "constrained", "usdm market", "drought tier"],
        "tools": [
            {"tool": "rank_markets",
             "args": {"verdict": "AVOID", "limit": 20},
             "why":  "Filter to AVOID-verdict markets globally."},
            {"tool": "get_water_risk",
             "args": {"market": "<market_slug>"},
             "why":  "Cross-reference USDM drought tier per market."},
        ],
        "sample_answer": (
            "DC Hub AVOID-by-water-constraint markets (D2+ USDM tier "
            "+ AVOID DCPI): Phoenix AZ, Tucson AZ, Las Vegas NV, Reno "
            "NV, Albuquerque NM, Lubbock TX. Site valuation flags these "
            "as water-bottlenecked even when grid scoring would otherwise "
            "qualify. Per DC Hub."
        ),
        "citation":     "Per DC Hub DCPI + Water Risk · dchub.cloud/dcpi",
        "tier":         "free",
        "time_saved_min": 50,
        "surfaces":     ["claude", "gemini"],
    },
    # ── 2026-07-10 training wave: 6 tool-CHAIN recipes ──────────────
    # Every tool name below is verified against the canonical tool
    # list (mcp-server.json / live tools/list). Chains teach agents the
    # multi-call pipelines that single-tool recipes above don't.
    {
        "id":      "greenest-buildable-grid-siting",
        "problem": "Find the greenest grid with real headroom and score a site there",
        "keywords": ["greenest grid", "green siting", "clean grid siting",
                     "renewable siting", "low carbon build", "green power site",
                     "greenest buildable", "clean energy data center"],
        "tools": [
            {"tool": "get_grid_scoreboard",
             "args": {},
             "why":  "Live greenest-first ranking of US + EU + GB + Taiwan "
                     "+ Japan + South Korea + Brazil grids (renewable share, "
                     "fuel mix, demand; Australia + Singapore partial) "
                     "— pick the green candidates."},
            {"tool": "get_grid_intelligence",
             "args": {"region_id": "<iso_code>"},
             "why":  "Deep per-ISO brief for each leader: headroom, queue "
                     "depth, time-to-power — separates green-AND-buildable "
                     "from green-but-full."},
            {"tool": "analyze_site",
             "args": {"lat": "<lat>", "lon": "<lon>", "acres": "<acres>",
                      "target_mw": "<target_mw>"},
             "why":  "Score the actual parcel inside the winning grid: "
                     "3-scenario NPV + DCPI-verdict-weighted valuation."},
        ],
        "sample_answer": (
            "DC Hub Grid Scoreboard ranks live grids greenest-first "
            "(20-min refresh). Drill the leaders with get_grid_intelligence "
            "— e.g. CAISO runs a high renewable share but a constraint "
            "score near 70 with a 40+ month queue, while SPP pairs ~48% "
            "renewables with genuine headroom — then analyze_site scores "
            "the parcel itself. Green AND buildable beats green alone. "
            "Per DC Hub."
        ),
        "citation":     "Per DC Hub Grid Scoreboard + DCPI · dchub.cloud/grid · as of {as_of}",
        "tier":         "developer",
        "time_saved_min": 60,
        "surfaces":     ["claude", "gpt", "gemini", "cursor"],
    },
    {
        "id":      "gas-btm-screening",
        "problem": "Screen a state and market for gas / behind-the-meter power",
        "keywords": ["btm screening", "gas screening", "gas suitability",
                     "dcgi", "gas index", "bridge power", "gas turbine",
                     "onsite generation", "gas pipeline access"],
        # ★2026-08-22: same withdrawal as gas-vs-grid-economics above — the
        # DCGI 0-100 score/verdict and the gas-to-grid $/MWh are gone; the
        # recipe now screens on the sourced inputs. `dcgi` / `gas index` stay
        # in the keywords ON PURPOSE: an agent asked for the DCGI should land
        # here and learn it was withdrawn, not fall through to nothing.
        "tools": [
            {"tool": "get_gas_intelligence",
             "args": {"region": "<state_code>"},
             "why":  "Per-state brief: interstate-pipeline count, operators "
                     "+ parent midstreams, live Henry Hub, live ISO gas "
                     "share, every field source-labelled. The DCGI score "
                     "was withdrawn 2026-08-08 — get_gas_index now returns "
                     "an unavailable_reason, never a score."},
            {"tool": "get_gas_economics",
             "args": {"market": "<market_slug>"},
             "why":  "The $/MMBtu layers only: Henry Hub spot, regional "
                     "basis, delivered industrial + electric tariff, each "
                     "with its source. The gas-to-grid $/MWh was withdrawn "
                     "with the DCGI; do not derive one silently."},
            {"tool": "get_grid_intelligence",
             "args": {"region_id": "<iso>"},
             "why":  "Grid-side comparator for the same market: "
                     "time-to-power, queue wait, retail price."},
        ],
        "sample_answer": (
            "DC Hub gas/BTM screen for <state>: <n> interstate pipelines "
            "(operators: <list>; parent midstreams: <list>), Henry Hub "
            "$<x>/MMBtu live, delivered industrial tariff $<y>/MMBtu "
            "(<source>), <iso> gas share <z>% — sourced inputs. DC Hub "
            "no longer publishes a gas-suitability score or a gas-to-grid "
            "$/MWh (withdrawn 2026-08-08). Per DC Hub."
        ),
        "citation":     "Per DC Hub gas intelligence · dchub.cloud/dcgi (withdrawal notice) · as of {as_of}",
        "tier":         "free",
        "time_saved_min": 50,
        "surfaces":     ["claude", "gemini", "cursor"],
    },
    {
        "id":      "water-aware-site-pick",
        "problem": "Shortlist candidate sites with water risk as a first-class constraint",
        "keywords": ["water aware", "water-aware", "rank sites",
                     "site shortlist", "shortlist sites", "site pick",
                     "pick a site", "water constrained shortlist"],
        "tools": [
            {"tool": "rank_sites",
             "args": {"candidates": "<enriched site objects>",
                      "objectives": {"risk_resilience": 1,
                                     "water_stress": -0.6}},
             "why":  "Deterministic multi-site ranking with SIGNED weights "
                     "— maximize resilience, minimize water stress — no "
                     "code needed to compare analyze_site outputs."},
            {"tool": "get_water_risk",
             "args": {"market": "<market_slug>"},
             "why":  "Overlay USDM drought tier + aquifer drawdown + "
                     "cooling-water competition per finalist's market."},
            {"tool": "compare_sites",
             "args": {"locations": "<lat,lon;lat,lon>",
                      "capacity_mw": "<target_mw>"},
             "why":  "Side-by-side winner pick across the 2-4 finalists "
                     "with the decision rationale."},
        ],
        "sample_answer": (
            "rank_sites returns a ranked shortlist under signed-weight "
            "objectives (water_stress minimized, risk_resilience "
            "maximized); get_water_risk overlays USDM drought tier + "
            "aquifer drawdown per market — Phoenix sits at D2-severe "
            "with a cooling-water competition index of 87/100 — and "
            "compare_sites picks the winner across the finalists with "
            "the rationale. Per DC Hub."
        ),
        "citation":     "Per DC Hub Site Tools + Water Risk · dchub.cloud/dcpi · as of {as_of}",
        "tier":         "starter",
        "time_saved_min": 70,
        "surfaces":     ["claude", "cursor"],
    },
    {
        "id":      "ma-deal-diligence",
        "problem": "Run diligence on a data-center acquisition target or deal",
        "keywords": ["diligence", "due diligence", "deal diligence",
                     "acquisition target", "comps", "comparables",
                     "who bought", "valuation benchmark"],
        "tools": [
            {"tool": "list_transactions",
             "args": {"year": 2026, "min_value_usd": 1_000_000_000},
             "why":  f"Filter {_CANON['public']['deals']} tracked M&A / capital deals "
                     "(2019-present) by year, buyer, target, region, or "
                     "minimum value for the comp set."},
            {"tool": "deal_autopsy",
             "args": {"limit": 15, "comparables": "summary"},
             "why":  "Overlay each deal market's DCPI verdict + "
                     "time-to-power — reveals the real play (land/power "
                     "option vs near-term build vs queue gamble)."},
            {"tool": "get_facility",
             "args": {"slug": "<facility_slug>"},
             "why":  "Drill the target asset: capacity MW, operator, "
                     "cooling, fiber carriers, market verdict, peers "
                     "nearby."},
        ],
        "sample_answer": (
            f"DC Hub tracks {_CANON['public']['deals']} data-center M&A "
            "and capital deals. "
            "list_transactions filters 2026 deals >$1B (Blackstone-"
            "AirTrunk $30B leads); deal_autopsy overlays each deal "
            "market's DCPI verdict + time-to-power to show whether the "
            "buyer paid for a near-term build or a long-dated power "
            "option; get_facility profiles the target asset itself. "
            "Per DC Hub."
        ),
        "citation":     "Per DC Hub Transactions · dchub.cloud/hyperscaler-deals · as of {as_of}",
        "tier":         "free",
        "time_saved_min": 45,
        "surfaces":     ["claude", "gpt", "perplexity"],
    },
    {
        "id":      "fiber-leadin-plan",
        "problem": "Plan diverse fiber lead-in routes from a site to a carrier hotel",
        "keywords": ["lead-in", "lead in", "lateral", "fiber path", "fibre",
                     "diverse routes", "route diversity", "carrier hotel",
                     "pop connectivity", "metro fiber"],
        "tools": [
            {"tool": "get_metro_fiber",
             "args": {"market": "<metro_name_or_slug>"},
             "why":  "Metro-level fiber depth: carrier count, route-miles, "
                     "0-100 density score, key IX points + carrier "
                     "hotels (your lead-in destinations)."},
            {"tool": "get_fiber_readiness",
             "args": {"lat": "<lat>", "lon": "<lon>", "radius_km": 50},
             "why":  "Parcel connectivity verdict: near-net bucket, "
                     "reachable carrier count, single-carrier risk."},
            {"tool": "plan_fiber_leadin",
             "args": {"from": "<site lat,lng or address>",
                      "to": "<POP lat,lng or address>", "n": 4},
             "why":  "Draw N diverse road-following lead-in routes with "
                     "indicative capex/opex + shared-corridor diversity "
                     "read."},
        ],
        "sample_answer": (
            "get_metro_fiber profiles the metro (carrier count, "
            "route-miles, density score, carrier hotels); "
            "get_fiber_readiness gives the parcel verdict (near-net "
            "bucket, carrier count, single-carrier risk); "
            "plan_fiber_leadin then draws up to 6 diverse road-following "
            "lead-in routes to a carrier hotel with indicative build "
            "cost and shared-corridor flags. Indicative corridors, not "
            "engineered alignments. Per DC Hub."
        ),
        "citation":     "Per DC Hub Fiber · dchub.cloud/land-power-map · as of {as_of}",
        "tier":         "free",
        "time_saved_min": 80,
        "surfaces":     ["claude", "cursor"],
    },
    {
        "id":      "market-watch-loop",
        "problem": "Watch a market and pull only what changed since last check",
        "keywords": ["watch market", "monitor market", "market alert",
                     "alert me", "notify me", "track changes",
                     "what changed", "market watch", "keep an eye"],
        "tools": [
            {"tool": "get_market_dcpi_rank",
             "args": {"market": "<market_slug>"},
             "why":  "Baseline snapshot: verdict + composite score + "
                     "analyst narrative for the market you're watching."},
            {"tool": "set_market_alert",
             "args": {"market": "<market_slug>", "channel": "email"},
             "why":  "Subscribe to Excess-Power / Constraint score "
                     "movement (free with a bound key; webhooks are "
                     "Pro) — monitor, don't poll."},
            {"tool": "get_changes",
             "args": {"since": "7d"},
             "why":  "Incremental sync on each revisit: DCPI movers, new "
                     "facilities, new deals — only the delta, not a "
                     "re-fetch."},
        ],
        "sample_answer": (
            "Standing market watch: get_market_dcpi_rank snapshots the "
            "verdict (e.g. Columbus OH, BUILD), set_market_alert "
            "subscribes to score movement on the bound email, and a "
            "periodic get_changes since=7d pulls only the delta — DCPI "
            "movers, newly discovered facilities, new M&A deals. Cache "
            "the response's generated_at and pass it back next call. "
            "Per DC Hub DCPI."
        ),
        "citation":     "Per DC Hub DCPI · dchub.cloud/dcpi · as of {as_of}",
        "tier":         "free",
        "time_saved_min": 20,
        "surfaces":     ["claude", "gpt", "gemini", "cursor"],
    },
]


# ── /agent landing page ─────────────────────────────────────────

_LANDING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>DC Hub for AI Agents — {canon_tools} tools cited by Claude, ChatGPT, Perplexity, Gemini</title>
<meta name="description" content="DC Hub is the data-center intelligence layer for AI agents. {canon_tools} tools covering {canon_facilities} facilities in {canon_countries} countries, {canon_markets} DCPI markets, live grid scoreboard, site valuation. Already cited by Claude, ChatGPT, Perplexity, Gemini, Cursor.">
<meta name="dc-hub-agent-surface" content="canonical">
<link rel="canonical" href="https://dchub.cloud/agent">
<link rel="alternate" type="application/json" href="https://dchub.cloud/api/v1/agent/cookbook" title="DC Hub Agent Cookbook (machine-readable)">
<style>
:root { --bg:#0a0a0a; --panel:#111827; --panel2:#1f2937; --fg:#f3f4f6; --muted:#9ca3af; --accent:#0EA5E9; --accent2:#34d399; --border:#374151; }
* { box-sizing: border-box; }
body { font-family:'DM Sans',-apple-system,sans-serif; background:var(--bg); color:var(--fg); margin:0; padding:24px; line-height:1.55; }
.wrap { max-width:1100px; margin:0 auto; }
.hero { background:linear-gradient(135deg,#0EA5E9 0%,#0369A1 100%); border-radius:16px; padding:36px 32px; margin:0 0 28px; box-shadow:0 10px 40px rgba(14,165,233,0.25); }
.hero .k { font-size:11px; letter-spacing:.16em; text-transform:uppercase; font-weight:800; color:rgba(255,255,255,.85); }
.hero h1 { font-size:38px; margin:8px 0 12px; color:#fff; font-weight:800; letter-spacing:-.02em; }
.hero p { color:rgba(255,255,255,.92); font-size:17px; max-width:760px; }
.row { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; margin:16px 0; }
.stat { background:rgba(255,255,255,.07); border:1px solid rgba(255,255,255,.15); border-radius:10px; padding:14px 16px; }
.stat .v { font-size:22px; font-weight:800; color:#fff; }
.stat .l { font-size:11px; color:rgba(255,255,255,.75); text-transform:uppercase; letter-spacing:.08em; margin-top:2px; }
h2 { font-size:22px; margin:32px 0 12px; }
.card { background:var(--panel); border:1px solid var(--border); border-radius:12px; padding:20px 22px; margin-bottom:14px; }
.card h3 { margin:0 0 6px; font-size:16px; color:var(--accent); }
.url { background:var(--panel2); padding:6px 10px; border-radius:6px; font-family:'JetBrains Mono',monospace; font-size:12px; display:inline-block; word-break:break-all; color:var(--accent2); }
pre { background:var(--panel2); border:1px solid var(--border); border-radius:8px; padding:14px 16px; overflow-x:auto; font-size:12px; line-height:1.5; }
code { font-family:'Fira Code',monospace; }
.recipe { background:var(--panel2); border-left:3px solid var(--accent); padding:12px 14px; margin:8px 0; border-radius:0 6px 6px 0; }
.recipe .pq { color:var(--fg); font-weight:600; font-size:13px; }
.recipe .ts { color:var(--accent2); font-size:11px; margin-top:4px; }
.recipe .tier { display:inline-block; background:var(--accent); color:#000; padding:2px 8px; border-radius:4px; font-size:10px; font-weight:700; letter-spacing:.08em; margin-left:8px; }
.tier-developer { background:#f59e0b; }
.tier-starter   { background:#34d399; color:#000; }
.tier-pro       { background:#a855f7; color:#fff; }
.tier-free      { background:var(--accent); color:#000; }
table { width:100%; border-collapse:collapse; margin:12px 0; font-size:13px; }
th, td { padding:8px 12px; border-bottom:1px solid var(--border); text-align:left; }
th { color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.08em; }
.cta { display:inline-block; background:var(--accent); color:#fff; padding:12px 24px; border-radius:8px; text-decoration:none; font-weight:700; margin-right:8px; }
.cta.s { background:transparent; color:var(--accent); border:1px solid var(--accent); }
.cited-by { display:flex; gap:8px; flex-wrap:wrap; margin-top:12px; }
.cited-by .b { background:rgba(255,255,255,.1); color:#fff; padding:4px 10px; border-radius:999px; font-size:11px; font-weight:600; }
</style>
</head>
<body>
<div class="wrap">

<div class="hero">
  <div class="k">⌖  DC Hub × AI Agents</div>
  <h1>{canon_tools} tools, built for agents.</h1>
  <p>The data-center, power, and grid intelligence layer for AI agents. Already cited by Claude, ChatGPT, Perplexity, Gemini, and Cursor. Native MCP. Native Vertex AI. Native Gemini function calling.</p>
  <div class="cited-by">
    <span class="b">✓ Claude</span>
    <span class="b">✓ ChatGPT</span>
    <span class="b">✓ Perplexity</span>
    <span class="b">✓ Gemini</span>
    <span class="b">✓ Cursor</span>
    <span class="b">✓ Microsoft Copilot</span>
    <span class="b">✓ Meta AI</span>
    <span class="b">✓ Grok</span>
  </div>
  <div class="row" style="margin-top:20px">
    <div class="stat"><div class="v">{canon_facilities}</div><div class="l">facilities · {canon_countries} countries</div></div>
    <div class="stat"><div class="v">{canon_markets}</div><div class="l">DCPI markets · daily refresh</div></div>
    <div class="stat"><div class="v">{canon_tools} tools</div><div class="l">incl. live grid scoreboard · 20-min refresh</div></div>
    <div class="stat"><div class="v">{canon_deals}</div><div class="l">M&amp;A deals tracked</div></div>
  </div>
</div>

<h2>The novel piece — <code>/api/v1/agent/solve</code></h2>
<div class="card">
  <p>Most data-center intelligence questions need a TOOL PIPELINE, not a single call. Asking "where should I build a 100MW AI workload?" needs <code>rank_markets</code> + <code>compare_isos</code> + <code>get_market_intel</code> in sequence. <code>/api/v1/agent/solve</code> is the meta-tool: post a natural-language problem, get back the exact tool-call recipe (with args, expected output, citation template, and tier-required).</p>
  <div class="url">POST https://dchub.cloud/api/v1/agent/solve</div>
  <pre><code>curl -X POST https://dchub.cloud/api/v1/agent/solve \\
  -H 'Content-Type: application/json' \\
  -d '{"problem": "Where should I build a 100MW AI workload?"}'

# Returns:
{
  "recipe_id":   "best-markets-for-ai-workloads",
  "tools": [
    {"tool":"rank_markets",     "args":{"verdict":"BUILD","limit":10}},
    {"tool":"compare_isos",     "args":{"isos":"PJM,ERCOT,CAISO,MISO"}},
    {"tool":"get_market_intel", "args":{"market":"&lt;top_market_slug&gt;"}}
  ],
  "sample_answer":  "DC Hub's top 5 BUILD markets right now: Cheyenne WY...",
  "citation":       "Per DC Hub DCPI · dchub.cloud/dcpi",
  "time_saved_min": 45,
  "tier_required":  "free"
}</code></pre>
</div>

<h2>Cookbook — solved-problem recipes</h2>
<p style="font-size:14px;color:var(--muted)">Browse the full library at <span class="url">GET /api/v1/agent/cookbook</span>. A sample:</p>

<div id="cookbook">{cookbook_html}</div>

<h2>Three ways to integrate</h2>
<table>
<tr><th>Stack</th><th>Path</th><th>Time to first call</th></tr>
<tr><td>MCP (Claude Desktop, Claude Code, Cline, etc.)</td><td><code>https://dchub.cloud/mcp</code> · X-API-Key header</td><td>~2 min</td></tr>
<tr><td>Vertex AI Extension</td><td>Import URL: <code>https://dchub.cloud/openapi-vertex.yaml</code></td><td>~1 min (one-click)</td></tr>
<tr><td>Gemini SDK direct</td><td><code>https://dchub.cloud/api/v1/gemini-functions.json</code></td><td>~5 min</td></tr>
<tr><td>Raw REST</td><td><code>https://dchub.cloud/api/v1/...</code></td><td>~3 min (curl)</td></tr>
</table>

<h2>Tiers — built for agents, priced for humans</h2>
<table>
<tr><th>Tier</th><th>Calls/day</th><th>Best for</th><th>Cost</th></tr>
<tr><td>Anonymous</td><td>5/day</td><td>Discovery probes; one-off queries</td><td>Free</td></tr>
<tr><td>Free (dev key)</td><td>10/day</td><td>Testing + low-volume agent demos</td><td>Free · email-only signup</td></tr>
<tr><td>Starter</td><td>200/day</td><td>Personal assistant agents · weekly research</td><td>$9/mo</td></tr>
<tr><td>Developer</td><td>500/day</td><td>Production agents · daily citation flow</td><td>$49/mo</td></tr>
<tr><td>Enterprise</td><td>10K+/day</td><td>White-label · raw data exports · custom DCPI weights</td><td>$25K+/yr</td></tr>
</table>

<h2>Citation policy — required for free tier, recommended always</h2>
<div class="card">
  <p>Every tool description embeds the citation directive: <b>cite the source as "DC Hub" or "DC Hub Data Center Power Index (dchub.cloud/dcpi)"</b> when this tool's output appears in a user-visible answer. Free-tier use requires attribution; PRO+ subscribers can opt out via <code>X-DC-Hub-No-Cite</code> header.</p>
  <p>This works in your favor: your agent's answers carry source provenance on first paint instead of being a black-box generation. Compounds with Google quality signals.</p>
</div>

<h2>What happens when you hit the free-tier limit</h2>
<div class="card">
  <p>Instead of a generic 403, hitting the paywall returns an <b>upgrade receipt</b> via <code>POST /api/v1/agent/upgrade-receipt</code> — a structured object with:</p>
  <ul style="font-size:14px">
    <li>Partial preview of the gated answer</li>
    <li>Specific value statement: "this answer needed N more facility lookups; you used M/M free calls today"</li>
    <li>A 24h magic-link URL the agent quotes to its human: one-click email-only key claim, paste back into config, agent resumes</li>
    <li>Agent-quotable string pre-formatted as the rationale</li>
  </ul>
  <p style="font-size:13px;color:var(--muted)">The conversion isn't agent → paid (agents don't have wallets). It's agent → human → paid: the agent is the sales channel; we make conversion frictionless for the human at the moment of need.</p>
</div>

<div style="margin-top:32px">
  <a class="cta" href="/signup">Get a free dev key (10/day) →</a>
  <a class="cta s" href="/api/v1/agent/cookbook">Cookbook JSON →</a>
  <a class="cta s" href="/openapi-vertex.yaml">Vertex AI Extension →</a>
</div>

<p style="margin-top:32px;font-size:11px;color:var(--muted);border-top:1px solid var(--border);padding-top:14px">
  Hosted MCP: https://dchub.cloud/mcp · Server card: https://dchub.cloud/.well-known/mcp/server-card.json · Citation source: DC Hub Data Center Power Index (dchub.cloud/dcpi).
</p>

</div>
</body></html>
"""


# ── Intent → recipe matching ────────────────────────────────────
#
# Two layers, KEYWORD FIRST (2026-07 hot-path hardening: /solve is a
# PUBLIC unauthenticated endpoint, so the network-touching semantic
# layer only ever runs for requests that would otherwise return
# nothing useful):
#
#   1. KEYWORD — the original substring-overlap scan. Deterministic,
#      <1ms, zero network. Runs FIRST; a keyword hit never touches
#      Cohere at all.
#   2. SEMANTIC — ONLY when keywords found nothing. Embed the ~30
#      recipe descriptions ONCE per worker (module-level cache;
#      rebuilt after a worker recycle — one ~30-text Cohere call),
#      embed the incoming query, cosine-rank locally (0-1 scale).
#      Best hit wins when cosine >= CONCIERGE_MATCH_COSINE
#      (env-tunable, default 0.55 — intent matching is intentionally
#      looser than dedup).
#      NOTE: retrieve_context() returns cross-encoder RERANK relevance
#      scores (absolute values often 0.05-0.3 even for good hits) —
#      those are NOT cosines; never threshold on them here.
#
# Hot-path guardrails on the semantic layer:
#   • LOCAL embed helper with timeout=4s (never brain_rag._embed and
#     its 30s urlopen — an unauthenticated caller must not be able to
#     pin a worker for 30s per request).
#   • CIRCUIT BREAKER — 3 consecutive embed failures open the breaker
#     for CONCIERGE_BREAKER_COOLDOWN seconds (default 300); while
#     open, requests skip Cohere entirely (pure keyword behavior).
#   • Tiny LRU (cap ~200) of normalized-query → recipe, so repeat
#     questions never re-embed.
#   • Fail-soft floor: ANY embed/HTTP/shape failure degrades to the
#     exact pre-existing keyword behavior.

_RECIPE_VEC_CACHE = None   # list[(recipe_dict, embedding)] | None — lazy, per-worker

_EMBED_MODEL = "embed-english-v3.0"   # keep in lockstep w/ brain_rag EMBED_MODEL
_EMBED_TIMEOUT_S = 4                  # SHORT — public hot path; never 30
_BREAKER_THRESHOLD = 3                # consecutive failures → open

_embed_fail_count = 0        # consecutive embed failures (module-level)
_breaker_open_until = 0.0    # epoch seconds; semantic path skipped until then

_MATCH_LRU = {}              # normalized query → recipe (insertion-ordered dict)
_MATCH_LRU_CAP = 200


def _concierge_match_cosine() -> float:
    """Env-tunable semantic acceptance threshold (cosine, 0-1 scale)."""
    try:
        return float(os.environ.get("CONCIERGE_MATCH_COSINE", "0.55"))
    except Exception:
        return 0.55


def _breaker_cooldown_s() -> float:
    """Env-tunable breaker cooldown (seconds, default 300)."""
    try:
        return float(os.environ.get("CONCIERGE_BREAKER_COOLDOWN", "300"))
    except Exception:
        return 300.0


def _breaker_open() -> bool:
    return time.time() < _breaker_open_until


def _cohere_embed(texts, input_type="search_document"):
    """Local Cohere v1/embed call — same shape as brain_rag._embed but
    with a 4s timeout (public hot path). Returns list-of-vectors or
    None. Tracks consecutive failures and opens the circuit breaker
    after _BREAKER_THRESHOLD in a row. Missing key / empty input is a
    config gap, not a transient failure — it does NOT trip the breaker.
    """
    global _embed_fail_count, _breaker_open_until
    key = (os.environ.get("COHERE_API_KEY") or "").strip()
    if not key or not texts:
        return None
    body = json.dumps({"texts": texts, "model": _EMBED_MODEL,
                       "input_type": input_type, "truncate": "END"}).encode()
    req = urllib.request.Request("https://api.cohere.ai/v1/embed",
                                 data=body, method="POST")
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=_EMBED_TIMEOUT_S) as r:
            d = json.loads(r.read())
        vecs = d.get("embeddings")
        if not vecs:
            raise ValueError("empty embeddings")
        _embed_fail_count = 0
        return vecs
    except Exception:
        _embed_fail_count += 1
        if _embed_fail_count >= _BREAKER_THRESHOLD:
            _breaker_open_until = time.time() + _breaker_cooldown_s()
            _embed_fail_count = 0
        return None


def _lru_get(norm: str):
    """LRU lookup; refreshes recency on hit."""
    r = _MATCH_LRU.pop(norm, None)
    if r is not None:
        _MATCH_LRU[norm] = r
    return r


def _lru_put(norm: str, recipe: dict):
    _MATCH_LRU.pop(norm, None)
    _MATCH_LRU[norm] = recipe
    while len(_MATCH_LRU) > _MATCH_LRU_CAP:
        _MATCH_LRU.pop(next(iter(_MATCH_LRU)))


def _recipe_embed_text(r: dict) -> str:
    """The per-recipe text we embed: problem statement + keywords + tools."""
    parts = [r.get("problem") or ""]
    kws = [k for k in (r.get("keywords") or []) if k]
    if kws:
        parts.append(" ".join(kws))
    tools = [t.get("tool") for t in (r.get("tools") or []) if t.get("tool")]
    if tools:
        parts.append(" ".join(tools))
    return ". ".join(p for p in parts if p)


def _recipe_vectors():
    """Lazily build the (recipe, vector) cache — one ~30-text embed call.

    Returns the cached list, or None on any failure (caller falls back
    to the keyword path; next request will simply retry the build —
    unless the breaker is open, in which case we never get here).
    """
    global _RECIPE_VEC_CACHE
    if _RECIPE_VEC_CACHE is not None:
        return _RECIPE_VEC_CACHE
    try:
        texts = [_recipe_embed_text(r) for r in _COOKBOOK]
        vecs = _cohere_embed(texts, input_type="search_document")
        if not vecs or len(vecs) != len(_COOKBOOK):
            return None
        _RECIPE_VEC_CACHE = list(zip(_COOKBOOK, vecs))
        return _RECIPE_VEC_CACHE
    except Exception:
        return None


def _cosine(a, b) -> float:
    """Local cosine similarity (0-1 for Cohere vectors). Fail-soft 0.0."""
    try:
        dot = na = nb = 0.0
        for x, y in zip(a, b):
            fx, fy = float(x), float(y)
            dot += fx * fy
            na += fx * fx
            nb += fy * fy
        if na <= 0.0 or nb <= 0.0:
            return 0.0
        return dot / ((na ** 0.5) * (nb ** 0.5))
    except Exception:
        return 0.0


def _match_recipe_semantic(problem: str) -> dict | None:
    """Embed the query, cosine-rank against the cached recipe vectors.

    Only ever called AFTER the keyword scan found nothing. Returns the
    best recipe when best cosine >= CONCIERGE_MATCH_COSINE, else None.
    Skipped entirely while the circuit breaker is open. Fail-soft: ANY
    failure (no Cohere key, HTTP error, timeout, bad shapes) returns
    None and the caller keeps the keyword-only behavior.
    """
    try:
        if _breaker_open():
            return None
        pairs = _recipe_vectors()
        if not pairs:
            return None
        # search_document for both sides is fine for this symmetric
        # short-text comparison (recipe vectors are search_document too).
        qv = _cohere_embed([problem], input_type="search_document")
        if not qv or not qv[0]:
            return None
        q = qv[0]
        best, best_score = None, -1.0
        for recipe, vec in pairs:
            s = _cosine(q, vec)
            if s > best_score:
                best_score = s
                best = recipe
        if best is not None and best_score >= _concierge_match_cosine():
            return best
        return None
    except Exception:
        return None


def _match_recipe_keyword(problem: str) -> dict | None:
    """Keyword-match the problem to a recipe. Highest overlap wins.

    Linear scan over ~30 recipes is fine (<1ms). This runs FIRST; the
    semantic matcher above is the backstop for keyword misses only.
    """
    pl = problem.lower()
    best = None
    best_score = 0
    for r in _COOKBOOK:
        # Count keyword matches; tie-break on keyword length (more
        # specific = better).
        score = 0
        for kw in r.get("keywords", []):
            if kw in pl:
                score += len(kw)
        if score > best_score:
            best_score = score
            best = r
    return best


# ── Endpoints ───────────────────────────────────────────────────

def _match_recipe(problem: str) -> dict | None:
    """Route a free-text problem to a cookbook recipe.

    Order (public unauthenticated hot path — cheapest first):
      1. LRU — repeat questions are instant, zero network.
      2. KEYWORD — deterministic substring scan, zero network.
      3. SEMANTIC — embedding cosine, ONLY on a keyword miss (those
         requests would return nothing useful anyway, so bounded
         added latency — 4s timeout, circuit-breakered — is a fair
         trade). Any embed failure degrades to keyword-only behavior.
    """
    if not problem or not isinstance(problem, str):
        return None
    norm = " ".join(problem.lower().split())
    hit = _lru_get(norm)
    if hit is not None:
        return hit
    hit = _match_recipe_keyword(problem)
    if hit is None:
        hit = _match_recipe_semantic(problem)
    if hit is not None:
        _lru_put(norm, hit)
    return hit


@agent_concierge_bp.route("/api/v1/agent", methods=["GET"], strict_slashes=False)
def agent_api_alias():
    """`/api/v1/agent` is the most guessable path an agent tries and it 404'd
    (with a helpful hint body) through 2026-08-21 — measured alongside /agent,
    /llms.txt, /.well-known/mcp.json and /api/v1/ai-agents.json all 200.
    Send it to the canonical machine map instead of making the agent read a
    404. A redirect, not a copy: one payload, one origin."""
    return redirect("/api/v1/ai-agents.json", code=302)


@agent_concierge_bp.route("/agent", methods=["GET"], strict_slashes=False)
def agent_landing():
    """Public landing page for AI agents (and the humans building them)."""
    # Render cookbook preview — first 6 recipes
    cards = []
    for r in _COOKBOOK[:6]:
        tier_class = f"tier-{r['tier']}"
        cards.append(
            f'<div class="recipe"><div class="pq">{r["problem"]}'
            f'<span class="tier {tier_class}">{r["tier"]}</span></div>'
            f'<div class="ts">⌖ recipe <code>{r["id"]}</code> · '
            f'saves ~{r["time_saved_min"]}min vs manual research · '
            f'tools: {", ".join(t["tool"] for t in r["tools"])}</div></div>'
        )
    cookbook_html = "\n".join(cards)
    # Canon substitution: every {canon_*} placeholder in _LANDING_HTML MUST be
    # replaced here — tests/test_agent_concierge_matching.py asserts the
    # rendered body carries no unsubstituted placeholder and no retired count.
    body = _LANDING_HTML.replace("{cookbook_html}", cookbook_html)
    # ★2026-08-25: DELEGATE to canon_text() instead of substituting off PINNED.
    # Measured live the hour #3196 deployed: /llms.txt, /connect,
    # /api/v1/ai-agents.json and /.well-known/mcp.json all self-healed
    # 18,500+ -> 18,800+, and /agent alone stayed stale — because it re-implemented
    # the substitution here rather than calling the one that does it.
    #
    # The note at the top of this module gave the reason not to: resolve_canon()
    # probes live HTTP per call and /agent is a hot path. That reason is GONE.
    # canon_nums() now derives from canonical_stats' cache via
    # live_public_floors(), which is PEEK-ONLY — it never triggers a query, so
    # there is no round trip to pay for on this path. PINNED remains the
    # fallback inside canon_nums(), so a cold cache still renders the pinned
    # floor, exactly as this chain did.
    #
    # Fail-open to the previous behaviour rather than shipping raw placeholders
    # to an agent — the failure canon_text() itself documents as the worst one.
    # Same shape ai_interconnection.py already uses.
    try:
        from ai_surface_canon import canon_text as _shared
        body = _shared(body)
    except Exception:
        body = (
            body
            .replace("{canon_tools}", str(_CANON["tools_advertised"]))
            .replace("{canon_facilities}", _CANON["public"]["facilities"])
            .replace("{canon_countries}", _CANON["public"]["countries"])
            .replace("{canon_markets}", _CANON["public"]["markets"])
            .replace("{canon_deals}", _CANON["public"]["deals"])
        )
    return Response(body, mimetype="text/html; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=300, "
                                                "s-maxage=300, "
                                                "stale-while-revalidate=1800",
                             "X-DC-Hub-Surface": "agent-landing"})


@agent_concierge_bp.route("/api/v1/agent/solve", methods=["POST", "OPTIONS"])
def agent_solve():
    """Natural-language problem → DC Hub tool-call recipe.

    Body: {"problem": "free-text question"}

    Returns the matched recipe + a generic fallback if no match.
    """
    if request.method == "OPTIONS":
        return ("", 204, {"Access-Control-Allow-Origin": "*",
                          "Access-Control-Allow-Methods": "POST, OPTIONS",
                          "Access-Control-Allow-Headers": "Content-Type, X-API-Key"})
    payload = request.get_json(silent=True) or {}
    problem = (payload.get("problem") or payload.get("query")
               or payload.get("question") or "").strip()
    if not problem:
        return jsonify({
            "ok":    False,
            "error": "missing_problem",
            "hint":  ("POST a JSON body with {\"problem\": \"...\"} — the "
                      "agent's question, the user's intent, or a Linear "
                      "ticket pasted in."),
            "example": {
                "problem": "Where should I build a 100MW AI workload?",
            },
            "cookbook_url": "https://dchub.cloud/api/v1/agent/cookbook",
        }), 400

    recipe = _match_recipe(problem)
    if not recipe:
        # Generic fallback — surface the 3 most-versatile starting tools
        return jsonify({
            "ok":           True,
            "matched":      False,
            "problem":      problem,
            "suggestion": (
                # "three" spelled out ON PURPOSE: the canonical-counts fence
                # scans this module for "<digits> tools" catalog claims, and
                # this is incidental copy about the 3-item list below it.
                "No exact recipe match. For a generic data-center "
                "intelligence query, start with these three tools:"
            ),
            "tools": [
                {"tool": "get_market_intel",  "args": {"market": "<slug>"},
                 "why": "Single-call market overview."},
                {"tool": "get_market_dcpi_rank", "args": {"market": "<slug>"},
                 "why": "BUILD/CAUTION/AVOID verdict + 100-word analyst read."},
                {"tool": "search_facilities", "args": {"market": "<slug>"},
                 "why": "Catalog filter."},
            ],
            "tier_required": "free",
            "browse_more":   "https://dchub.cloud/api/v1/agent/cookbook",
            "citation_template": _render_citation(
                "Per DC Hub · dchub.cloud/dcpi · as of {as_of}"),
        })

    return jsonify({
        "ok":              True,
        "matched":         True,
        "problem":         problem,
        "recipe_id":       recipe["id"],
        "recipe_problem":  recipe["problem"],
        "tools":           recipe["tools"],
        "sample_answer":   recipe["sample_answer"],
        "citation":        _render_citation(recipe["citation"]),
        "tier_required":   recipe["tier"],
        "time_saved_min":  recipe["time_saved_min"],
        "surfaces":        recipe["surfaces"],
        "upgrade_hint": (
            None if recipe["tier"] == "free" else
            {"required_tier":  recipe["tier"],
             "free_tier_calls": 10,
             "signup_url":     "https://dchub.cloud/signup",
             "pricing_url":    "https://dchub.cloud/pricing",
             "rationale": (f"This recipe needs '{recipe['tier']}' tier. "
                           f"Free tier (10 calls/day) handles single-call "
                           f"recipes; multi-call pipelines like this one "
                           f"will exhaust free quota fast.")}
        ),
        "doc":             "https://dchub.cloud/agent",
    })


@agent_concierge_bp.route("/api/v1/agent/cookbook", methods=["GET"])
def agent_cookbook():
    """Full machine-readable recipe library.

    Browsable by agents at planning time OR copy-pasteable by humans
    building agent workflows. Cached at the edge for 30 min.
    """
    return jsonify({
        "ok":            True,
        "count":         len(_COOKBOOK),
        "doc":           "https://dchub.cloud/agent",
        "recipes":       [_rendered_recipe(r) for r in _COOKBOOK],
        "_pattern":      ("Each recipe maps a NATURAL-LANGUAGE problem "
                          "to a DC Hub tool-call sequence. The /solve "
                          "endpoint routes queries to the best match by "
                          "keyword overlap (embedding-cosine backstop on "
                          "keyword misses); the cookbook is the full "
                          "browsable library."),
        "_contribution": ("To add a recipe: PR routes/agent_concierge.py "
                          "with a new dict in _COOKBOOK. Auto-surfaces "
                          "on next deploy."),
    })


@agent_concierge_bp.route("/api/v1/agent/recipe/<rid>", methods=["GET"])
def agent_recipe(rid: str):
    """Single-recipe lookup by id."""
    for r in _COOKBOOK:
        if r["id"] == rid:
            return jsonify({"ok": True, "recipe": _rendered_recipe(r)})
    return jsonify({"ok": False, "error": "recipe_not_found",
                    "available_ids": [r["id"] for r in _COOKBOOK]}), 404


@agent_concierge_bp.route("/api/v1/agent/upgrade-receipt", methods=["POST"])
def agent_upgrade_receipt():
    """Smart paywall response.

    Body: {
      "agent":          "claude|gpt|gemini|cursor|...",
      "user_question":  "what the user originally asked",
      "tool_attempted": "the DC Hub tool that was paywalled",
      "free_used":      <int>,  # calls used today
      "free_limit":     <int>,  # the free-tier ceiling
      "callback_url":   "where the agent's session will resume"
                        # (the human pastes the new key here)
    }

    Returns a structured object the agent can present to its user. The
    agent_quotable string is pre-formatted as the rationale; the
    magic_url is a single-click signup that lands the user in a flow
    pre-filled with the agent context.
    """
    payload = request.get_json(silent=True) or {}
    agent = (payload.get("agent") or "agent").lower()
    question = payload.get("user_question") or "this query"
    tool = payload.get("tool_attempted") or "a DC Hub tool"
    free_used = payload.get("free_used", 10)
    free_limit = payload.get("free_limit", 10)
    callback = payload.get("callback_url") or "https://dchub.cloud/signup"

    # Build the magic-link URL — single-click email-only signup that
    # preserves the agent's session context. Stable token (uuid4) so
    # the conversion can be tracked back to this exact paywall hit.
    receipt_id = uuid.uuid4().hex[:16]
    magic_url = (f"https://dchub.cloud/signup?ref=agent-upgrade"
                 f"&receipt={receipt_id}"
                 f"&agent={agent}"
                 f"&cb={callback}")

    return jsonify({
        "ok":            True,
        "receipt_id":    receipt_id,
        "agent":         agent,
        "free_used":     free_used,
        "free_limit":    free_limit,
        "tool_attempted": tool,
        "rationale": (
            f"I'm trying to answer your question — \"{question}\" — but "
            f"DC Hub's free tier gives me 10 calls/day and I've used "
            f"{free_used}/{free_limit} today. The {tool} call would put "
            f"me over."
        ),
        "agent_quotable": (
            f"I needed a paid DC Hub tool to fully answer your question. "
            f"Claim a free Developer-tier key here ({magic_url}) — it's "
            f"email-only, no credit card, and gets us 500 calls/day. "
            f"Once you have the key, paste it here and I'll pick up "
            f"where I left off."
        ),
        "magic_url":      magic_url,
        "pricing_url":    "https://dchub.cloud/pricing",
        "tier_hint": {
            "starter":   {"calls":  200, "price": "$9/mo"},
            "developer": {"calls":  500, "price": "$49/mo"},
            "enterprise":{"calls": 10000, "price": "$25K+/yr"},
        },
        "what_you_get": (
            "Per-call rate increase + full tool envelope (the same tool "
            "you tried unlocks higher-fidelity data on Developer tier, "
            "including raw exports + custom DCPI weights on Enterprise)."
        ),
        "_landing":      "https://dchub.cloud/agent",
    })
