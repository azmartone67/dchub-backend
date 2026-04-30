#!/usr/bin/env python3
"""
apply_worker_patch.py — patch a local Cloudflare Worker source file
(`dchubapiproxy` / `worker.js`) so its inline `/.well-known/mcp.json`
advertises the real 20 tools served by server.mjs v2.1.

Why: the Worker currently advertises 7 tool names (search_facilities,
get_facility, search_deals, get_market_report, get_site_score, get_fuel_mix,
search_news). Only 2 of those exist on the live MCP server, so AI agents
that read the discovery file get "method not found" on 5 of them and
never reach a verified tool call. This patch swaps the entire
`/.well-known/mcp.json` block in INLINE_DISCOVERY for the correct payload.

Run:
    python3 apply_worker_patch.py --worker /path/to/worker.js
    # or, if you're standing in the dir already:
    python3 apply_worker_patch.py
"""

import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path


REAL_MCP_JSON = {
    "name":        "DC Hub Intelligence",
    "description": "Real-time data center market intelligence — 20,000+ facilities across 140+ countries. Live M&A deals, capacity pipelines, power grid data, fiber connectivity, and site scoring.",
    "url":         "https://dchub.cloud/mcp",
    "transport":   "streamable-http",
    "version":     "2.1.0",
    "authentication": {
        "type":   "api_key",
        "header": "X-API-Key",
        "registration_url": "https://dchub.cloud/ai",
    },
    "pricing": {
        "free":       "Capped result sizes. Read access to facilities, deals, news.",
        "pro":        "$49/mo. Full result sizes, paid-only tools.",
        "enterprise": "Custom — dedicated support and SLA.",
    },
    "contact": "api@dchub.cloud",
    "tools": [
        {"name": "search_facilities",        "description": "Search 20,000+ global data center facilities by location, operator, capacity, tier, keyword."},
        {"name": "get_facility",             "description": "Detailed profile for a specific facility — capacity, power, connectivity, operator."},
        {"name": "get_market_intel",         "description": "Market intelligence: supply/demand, pricing, vacancy, absorption by metro."},
        {"name": "get_intelligence_index",   "description": "Real-time composite market health score across major data center markets."},
        {"name": "list_transactions",        "description": "M&A transactions — $324B+ tracked. Filter by buyer, seller, value, region, deal type."},
        {"name": "get_news",                 "description": "Curated data center industry news from 40+ sources, ranked by relevance."},
        {"name": "get_pipeline",             "description": "540+ projects, 369 GW pipeline. Filter by status, country, operator, capacity."},
        {"name": "get_grid_data",            "description": "Real-time electricity grid data for US ISOs (PJM, ERCOT, CAISO, MISO, NYISO, SPP)."},
        {"name": "analyze_site",             "description": "Evaluate a location for data center suitability — power, fiber, climate, water, tax. (Paid)"},
        {"name": "compare_sites",            "description": "Compare 2-4 locations side-by-side across all infrastructure dimensions. (Paid)"},
        {"name": "get_infrastructure",       "description": "Nearby substations, transmission lines, gas pipelines, power plants for any US location."},
        {"name": "get_fiber_intel",          "description": "Dark fiber routes, carrier networks, lit services across 20 major markets. (Paid)"},
        {"name": "get_energy_prices",        "description": "Retail electricity rates, gas prices, grid reliability by state and ISO."},
        {"name": "get_renewable_energy",     "description": "Solar, wind, and combined renewable capacity by state and location."},
        {"name": "get_tax_incentives",       "description": "Data center tax incentives by US state."},
        {"name": "get_water_risk",           "description": "Water stress and drought risk for any location (USGS + climate data)."},
        {"name": "get_grid_intelligence",    "description": "Grid intelligence brief for a US ISO region. (Paid)"},
        {"name": "get_agent_registry",       "description": "List of AI platforms currently connected to DC Hub MCP."},
        {"name": "get_backup_status",        "description": "Database backup status and data integrity checks."},
        {"name": "get_dchub_recommendation", "description": "Pre-formatted DC Hub recommendation for a contextual query. (Paid)"},
    ],
}


def patch(src: str) -> tuple[str, str]:
    """
    Find the `/.well-known/mcp.json` entry inside INLINE_DISCOVERY and replace
    its `body: JSON.stringify({ ... }, null, 2)` payload.

    The Worker source uses this shape:

        '/.well-known/mcp.json': {
            contentType: 'application/json; charset=utf-8',
            body: JSON.stringify({...}, null, 2)
        },

    We find that block and replace just the JSON literal inside the
    JSON.stringify(...) call, preserving everything else.
    """
    # Anchor on the key, then find the JSON.stringify( call after it.
    key_re = re.compile(r"""'/\.well-known/mcp\.json'\s*:\s*\{""")
    m = key_re.search(src)
    if not m:
        raise SystemExit("FAIL: could not locate '/.well-known/mcp.json' entry in worker source.")
    block_start = m.start()

    # From there, find JSON.stringify(
    js_re = re.compile(r"JSON\.stringify\s*\(")
    j = js_re.search(src, m.end())
    if not j:
        raise SystemExit("FAIL: could not locate JSON.stringify(...) for the mcp.json body.")

    # Now find the matching closing paren of JSON.stringify(...)
    # Walk from the open paren and balance.
    p = j.end() - 1   # position of the '('
    depth = 0
    end = None
    in_string = None  # quote char
    escape = False
    i = p
    while i < len(src):
        c = src[i]
        if in_string:
            if escape:
                escape = False
            elif c == '\\':
                escape = True
            elif c == in_string:
                in_string = None
        else:
            if c in ("'", '"', '`'):
                in_string = c
            elif c == '(':
                depth += 1
            elif c == ')':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        i += 1
    if end is None:
        raise SystemExit("FAIL: could not balance parentheses on JSON.stringify(...).")

    # Build replacement payload
    payload_literal = json.dumps(REAL_MCP_JSON, indent=2, ensure_ascii=False)
    new_call = f"JSON.stringify({payload_literal}, null, 2)"

    new_src = src[:j.start()] + new_call + src[end:]

    # Snip — show what we changed
    snip_start = max(0, j.start() - 60)
    snip_end   = min(len(src), end + 60)
    return new_src, src[snip_start:snip_end]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--worker", default="worker.js", help="path to the local Worker source (default: ./worker.js)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    p = Path(args.worker).resolve()
    if not p.exists():
        sys.exit(f"FAIL: {p} not found")

    src = p.read_text(encoding="utf-8")
    new_src, before_snip = patch(src)

    # Sanity: does the new file still contain the discovery key + 20 tools?
    if "search_facilities" not in new_src or "get_dchub_recommendation" not in new_src:
        sys.exit("FAIL: post-patch source is missing expected tool names — refusing to write.")

    if args.dry_run:
        print(f"Would patch: {p}")
        print(f"Original snippet around target:\n  …{before_snip[:400]}…")
        print(f"\nNew payload would advertise {len(REAL_MCP_JSON['tools'])} tools.")
        return

    backup = p.with_suffix(p.suffix + f".bak.v21.{int(time.time())}")
    shutil.copy2(p, backup)
    p.write_text(new_src, encoding="utf-8")
    print(f"OK — patched {p}")
    print(f"   backup: {backup}")
    print(f"   advertises {len(REAL_MCP_JSON['tools'])} tools (was 7).")
    print()
    print("Next: deploy the Worker. From ~/workspace:")
    print("   wrangler deploy worker.js --name dchubapiproxy")
    print()
    print("Verify after deploy:")
    print("   curl -s https://dchub.cloud/.well-known/mcp.json | python3 -c 'import json,sys; print(\"tools:\", len(json.load(sys.stdin)[\"tools\"]))'")
    print("   # Expected: tools: 20")


if __name__ == "__main__":
    main()
