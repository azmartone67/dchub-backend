#!/usr/bin/env python3
"""mcp_facts_export.py — generate the canonical MCP facts JSON from the Python SoTs.

THE CROSS-LANGUAGE BRIDGE. The dchub-mcp-server (Node) repo cannot import these
Python source-of-truth modules, so its manifests (smithery.yaml, README,
mcp-server.json, server.json) drift independently — that's how "Pro $199",
"countries 140", "EU ~12 zones", and stale tool counts kept reappearing.

This emits ONE JSON file — canonical/mcp_facts.json — holding the canonical
facts (pricing, calls/day, headline numbers, grid coverage, description) built
straight from tier_registry + canonical_stats. sync-tools-manifest.mjs then
renders every Node-repo surface from it instead of hand-maintaining copies.

NOT emitted here (Node owns them, code-derived):
  - version    → server.json .version
  - tool_count → server.mjs _registeredToolNames (the live tools/list count;
                 the backend catalog's LIVE_MCP_TOOL_COUNT=46 LAGS the gateway).

Run:  python3 mcp_facts_export.py   (re-run after ANY tier/number/coverage edit;
then commit canonical/mcp_facts.json in the mcp-server repo).
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tier_registry as t          # noqa: E402
import canonical_stats as c        # noqa: E402

PRICE_TIERS = ["starter", "developer", "pro", "team", "enterprise"]
DAILY_TIERS = ["free", "identified", "starter", "developer", "pro", "enterprise"]


def _markets_floor() -> str:
    n = int(c.get_canonical_stats().get("markets", 300))
    return f"{(n // 100) * 100}+"          # 311 -> "300+"


def build() -> dict:
    s = c.get_canonical_stats()
    return {
        "_generated_by": "dchub-backend/mcp_facts_export.py",
        "_warning": ("CANONICAL — DO NOT HAND-EDIT. Regenerate via "
                     "`python3 mcp_facts_export.py`. version + tool_count are "
                     "owned by sync-tools-manifest.mjs (server.json / server.mjs)."),
        "description": ("Real-time data-center, power-grid & energy intelligence "
                        "for AI agents — the only MCP server an LLM can both query "
                        "and cite."),
        "remote_url": "https://dchub.cloud/mcp",
        "repo": "github.com/azmartone67/dchub-mcp-server",
        "license": "CC-BY-4.0",
        "pricing_usd_month": {k: t.price(k) for k in PRICE_TIERS},
        "calls_per_day": {k: t.calls_per_day(k) for k in DAILY_TIERS},
        "numbers": {
            "facilities": c.facilities_phrase(),       # "21,000+"
            "countries": c.countries_phrase(),         # "170+"
            "markets": _markets_floor(),               # "300+"
            "deals": "3,000+",                         # canonical floor (live ~3,079)
            "substations": f'{int(s.get("substations", 126427)):,}',
            "pipeline_gw": int(s.get("pipeline_gw", 369)),
        },
        "grid_coverage": {
            "us_isos": int(s.get("isos", 7)),
            "eia_balancing_authorities": int(s.get("utility_bas", 43)),
            "eu_entsoe_zones": int(s.get("eu_zones", 24)),
            "phrase_full": c.grid_coverage_phrase("full"),
            "phrase_short": c.grid_coverage_phrase("short"),
        },
    }


def main() -> int:
    body = json.dumps(build(), indent=2) + "\n"
    # primary consumer = the Node mcp-server repo; secondary = a servable backend copy
    targets = [
        os.path.join(HERE, "..", "dchub-mcp-server", "canonical", "mcp_facts.json"),
        os.path.join(HERE, "static", ".well-known", "mcp_facts.json"),
    ]
    for path in targets:
        repo_dir = os.path.dirname(os.path.dirname(path))      # the repo root
        if not os.path.isdir(repo_dir):
            print(f"  skipped {os.path.normpath(path)} (repo not present)")
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        print(f"  wrote {os.path.normpath(path)}")
    print("\n" + body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
