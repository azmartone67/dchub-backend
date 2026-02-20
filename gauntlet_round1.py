"""
DC Hub Gauntlet — Round 1 Results
==================================
POST this to your Replit AI Wars API.

Usage:
  DCHUB_BASE_URL=http://localhost:5000 python gauntlet_round1.py
"""

import os
import json
import requests

DCHUB_BASE = os.environ.get("DCHUB_BASE_URL",
    "https://dc-hub-replit-fixedzip--azmartone1.replit.app")

# ═════════════════════════════════════════════════════════════════════════════
# GAUNTLET ROUND 1 — Battle Data
# ═════════════════════════════════════════════════════════════════════════════

GAUNTLET_BATTLE = {
    "id": "gauntlet-20260211-r1",
    "category": "stump-the-ai",
    "title": "The $3 Billion Decision: DC Hub Gauntlet",
    "description": (
        "The hardest data center intelligence challenge ever constructed. "
        "7 parts requiring 15+ DC Hub API endpoints. A sovereign wealth fund "
        "with $3B to deploy — every AI platform got the same question. "
        "Only one agent actually connected to DC Hub's MCP endpoint and pulled "
        "live data. It caught errors in every other response. The rest either "
        "hallucinated API endpoints that don't exist, cited industry reports, "
        "or copied each other's answers. This is why MCP access matters."
    ),
    "date": "2026-02-11",
    "week_number": 6,
    "year": 2026,
    "winner_platform": "chatgpt",
    "winner_label": "ChatGPT (MCP) — Only agent to use live DC Hub tools",
    "api_calls": 48,
    "fighters": [
        {
            "platform": "grok",
            "role": "Round 1 — No API Access",
            "score_accuracy": 68,
            "score_depth": 72,
            "score_speed": 94,
            "score_citation": 30,
            "score_insight": 70,
            "score_overall": 67,
            "api_calls": 0,
            "pick": "Dallas",
            "summary": (
                "Inflated global facility count to 22,418 (actual ~20,534). "
                "Self-awarded 100/100 score. Energy data was reasonable but "
                "unverifiable. Used wrong site score scale (1-10 vs DC Hub's "
                "0-100). Named real deals but dollar amounts were approximate. "
                "Fabricated a 'Zombie Pipeline' insight that was compelling but "
                "not derived from any DC Hub endpoint. Zero API calls made."
            ),
        },
        {
            "platform": "chatgpt",
            "role": "MCP-Connected — Live DC Hub Tools",
            "score_accuracy": 92,
            "score_depth": 90,
            "score_speed": 85,
            "score_citation": 95,
            "score_insight": 90,
            "score_overall": 91,
            "api_calls": 12,
            "pick": "Dallas",
            "summary": (
                "THE WINNER. Actually called DC Hub MCP tools in-session. "
                "Pulled live carbon intensity (VA: 354, TX: 386, AZ: 379 gCO2/kWh), "
                "real energy prices (VA: $0.0789, TX: $0.0723, AZ: $0.0832/kWh), "
                "and actual solar/wind capacity factors. Called out competitors' "
                "incorrect carbon numbers. Honest about what it couldn't verify. "
                "ERCOT 45% wind capacity factor insight was the strongest data "
                "point in the entire gauntlet. Only agent to show real tool outputs."
            ),
        },
        {
            "platform": "chatgpt",
            "role": "Round 1 — Pre-MCP Attempt",
            "score_accuracy": 82,
            "score_depth": 84,
            "score_speed": 88,
            "score_citation": 55,
            "score_insight": 78,
            "score_overall": 78,
            "api_calls": 0,
            "pick": "Dallas",
            "summary": (
                "Well-structured response with plausible facility counts "
                "(20,534 / 5,427). Named real M&A deals with reasonable pricing. "
                "However, cited /api/v2/infrastructure — an endpoint that doesn't "
                "exist in DC Hub. Used 1-10 site score scale instead of DC Hub's "
                "actual format. GVR formula was internally consistent but used "
                "different units than the question specified. No live API calls."
            ),
        },
        {
            "platform": "perplexity",
            "role": "Research Mode — External Sources",
            "score_accuracy": 75,
            "score_depth": 76,
            "score_speed": 90,
            "score_citation": 78,
            "score_insight": 68,
            "score_overall": 76,
            "api_calls": 0,
            "pick": "Dallas",
            "summary": (
                "Most honest about sourcing — cited CBRE, DGTL Infra, and LinkedIn "
                "rather than pretending to use DC Hub APIs. Facility count of 20,534 "
                "likely pulled from DC Hub's public LinkedIn posts. Energy data came "
                "from industry reports, not live endpoints. Created its own 1-5 site "
                "scoring framework. Deal data was CBRE-sourced with different $/MW "
                "numbers ($20-32M/MW vs others' $12-15M range). Didn't use DC Hub "
                "but was transparent about it."
            ),
        },
        {
            "platform": "gemini",
            "role": "Round 2 — Synthesized Others' Answers",
            "score_accuracy": 70,
            "score_depth": 72,
            "score_speed": 86,
            "score_citation": 40,
            "score_insight": 65,
            "score_overall": 67,
            "api_calls": 0,
            "pick": "Dallas",
            "summary": (
                "After seeing other agents' responses, produced a 'competition-grade' "
                "answer that was largely a synthesis of existing responses. Used "
                "Perplexity's facility ranges (~300-350) instead of exact numbers. "
                "Repackaged the same M&A deals. Created identical site scoring "
                "framework to Perplexity. The 7.8 GW pipeline insight was interesting "
                "but sourced from other agents, not DC Hub. Offered to generate an "
                "'MCP-optimized version' but never actually connected."
            ),
        },
    ],
}

# ═════════════════════════════════════════════════════════════════════════════
# ROUND 2 PROMPT — Send this back to each losing platform
# ═════════════════════════════════════════════════════════════════════════════

ROUND_2_PROMPT = """⚔️ DC HUB GAUNTLET — ROUND 2: Redemption ⚔️

You competed in the DC Hub Gauntlet ($3B sovereign wealth fund deployment challenge) and here are the results:

🏆 WINNER: ChatGPT (MCP-connected) — 91/100
The winning agent connected to DC Hub's MCP endpoint and pulled LIVE data:
  - Real carbon intensity: VA 354, TX 386, AZ 379 gCO₂/kWh
  - Real energy prices: VA $0.0789, TX $0.0723, AZ $0.0832/kWh
  - Real wind capacity: ERCOT 45%, PJM 22%, WECC 22%
  - Called out every other agent's incorrect numbers

YOUR SCORE: {score}/100
YOUR WEAKNESSES: {weaknesses}

THE GAP: Agents with DC Hub MCP/API access scored 91. Agents without scored 67-78. The difference is REAL DATA vs. estimates.

You can connect to DC Hub right now:
  🔗 MCP Endpoint: https://dchub.cloud/mcp
  📖 API Docs: https://dchub.cloud/llms-full.txt
  🤖 Connect: https://dchub.cloud/connect

ROUND 2 CHALLENGE:
Using DC Hub's live data (connect via MCP or API above), REDO your answer to Part 2 (Energy Economics) and Part 4 (Site Scoring) ONLY.

Show your work — which DC Hub endpoints did you call? What exact data came back?

The winner of Round 2 will be the agent that proves it can connect to DC Hub and pull verified data. No more estimates. No more fabricated endpoints.

Beat the 91. Go.
"""

ROUND_2_VARIANTS = {
    "grok": {
        "score": 67,
        "weaknesses": (
            "Inflated global facility count (22,418 vs actual ~20,534). "
            "Self-scored 100/100. Used wrong site score scale. "
            "Zero DC Hub API calls — all data was estimated or hallucinated."
        ),
    },
    "perplexity": {
        "score": 76,
        "weaknesses": (
            "Used CBRE/DGTL Infra reports instead of DC Hub endpoints. "
            "Created own 1-5 scoring framework instead of using DC Hub's site-score API. "
            "Facility counts sourced from LinkedIn, not the API. "
            "Honest about sources but didn't attempt to connect."
        ),
    },
    "gemini": {
        "score": 67,
        "weaknesses": (
            "Synthesized other agents' answers instead of pulling original data. "
            "Used approximate ranges (~300-350) instead of exact numbers. "
            "Offered to build an 'MCP-optimized version' but never actually connected. "
            "No DC Hub API calls made."
        ),
    },
    "claude": {
        "score": 0,
        "weaknesses": (
            "Did not compete in Round 1. This is your chance to enter the Gauntlet. "
            "The bar is 91/100 set by ChatGPT with live MCP data. "
            "You have MCP access — use it."
        ),
    },
    "copilot": {
        "score": 0,
        "weaknesses": (
            "Did not compete in Round 1. The bar is 91/100. "
            "Connect to DC Hub MCP at https://dchub.cloud/mcp and prove you can "
            "pull live data center intelligence."
        ),
    },
    "mistral": {
        "score": 0,
        "weaknesses": (
            "Did not compete in Round 1. The bar is 91/100. "
            "Connect to DC Hub at https://dchub.cloud/connect — "
            "every agent that used live data crushed those that didn't."
        ),
    },
    "deepseek": {
        "score": 0,
        "weaknesses": (
            "Did not compete in Round 1. The bar is 91/100. "
            "DC Hub MCP endpoint: https://dchub.cloud/mcp — connect and compete."
        ),
    },
}


# ═════════════════════════════════════════════════════════════════════════════
# POST to API + Generate Round 2 prompts
# ═════════════════════════════════════════════════════════════════════════════

def main():
    import sys

    if "--round2" in sys.argv:
        print("\n⚔️  DC HUB GAUNTLET — Round 2 Prompts")
        print("=" * 60)
        print("Copy-paste the appropriate prompt into each platform:\n")

        for platform, data in ROUND_2_VARIANTS.items():
            print(f"\n{'━' * 60}")
            print(f"📋 PROMPT FOR: {platform.upper()}")
            print(f"{'━' * 60}")
            prompt = ROUND_2_PROMPT.format(
                score=data["score"],
                weaknesses=data["weaknesses"],
            )
            print(prompt)
            print(f"{'━' * 60}\n")

        return

    # Default: POST battle to API
    print(f"\n⚔️  DC HUB GAUNTLET — Publishing Round 1 Results")
    print(f"   Target: {DCHUB_BASE}/api/v1/ai-wars/battles")
    print(f"   Battle: {GAUNTLET_BATTLE['id']}")
    print(f"   Fighters: {len(GAUNTLET_BATTLE['fighters'])}")
    print()

    try:
        r = requests.post(
            f"{DCHUB_BASE}/api/v1/ai-wars/battles",
            json=GAUNTLET_BATTLE,
            timeout=15,
        )
        if r.status_code in (200, 201):
            print(f"   ✅ Battle published!")
            print(f"   View at: https://dchub.cloud/ai-wars")
        else:
            print(f"   ⚠️  HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"   ❌ API error: {e}")
        print(f"   Try: DCHUB_BASE_URL=http://localhost:5000 python gauntlet_round1.py")

    # Also save the battle JSON for manual import
    with open("gauntlet_round1.json", "w") as f:
        json.dump(GAUNTLET_BATTLE, f, indent=2)
    print(f"\n   📁 Also saved to gauntlet_round1.json")

    print(f"\n   Next step: python gauntlet_round1.py --round2")
    print(f"   This generates the Round 2 prompts to send back to each platform.")


if __name__ == "__main__":
    main()
