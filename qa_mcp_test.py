#!/usr/bin/env python3
"""
DC Hub — MCP Tools QA Test
============================
Sends real JSON-RPC tool calls to the MCP server and validates responses.
Tests all 20 tools (Neon-direct + REST-proxy paths).

Usage:
    python qa_mcp_test.py                        # Railway (production)
    python qa_mcp_test.py --env replit           # Replit failover
    python qa_mcp_test.py --tool search_facilities   # single tool only
    python qa_mcp_test.py --quick               # fast subset (5 tools)

The MCP server proxies through Flask /mcp → port 8888.
All calls use the JSON-RPC 2.0 protocol.
"""

import os, sys, json, time, argparse, urllib.request, urllib.error
from datetime import datetime

TARGETS = {
    "railway": "https://dchub-backend-production.up.railway.app/mcp",
    "replit":  "https://dc-hub-replit-fixedzip--azmartone1.replit.app/mcp",
    "local":   "http://localhost:8080/mcp",
}

GREEN  = "\033[92m"; RED = "\033[91m"; YELLOW = "\033[93m"
CYAN   = "\033[96m"; RESET = "\033[0m"; BOLD = "\033[1m"

pass_count = fail_count = warn_count = 0
_call_id = 0

def _rpc(url, method, params=None, timeout=25):
    global _call_id
    _call_id += 1
    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "id": _call_id,
        "params": params or {}
    }
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST"
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            elapsed = round((time.time() - t0) * 1000)
            try:
                return r.status, json.loads(raw), elapsed
            except Exception:
                return r.status, raw, elapsed
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        elapsed = round((time.time() - t0) * 1000)
        try:
            return e.code, json.loads(raw), elapsed
        except Exception:
            return e.code, raw, elapsed
    except urllib.error.URLError as e:
        return 0, str(e), 0

def _tool_call(url, tool_name, arguments=None):
    """Call a tool via tools/call JSON-RPC method."""
    return _rpc(url, "tools/call", {
        "name": tool_name,
        "arguments": arguments or {}
    })

def _check(name, status, body, elapsed_ms, timeout_warn_ms=8000):
    global pass_count, fail_count, warn_count
    issues = []

    if status == 0:
        issues.append(f"connection error: {body}")
    elif status != 200:
        issues.append(f"HTTP {status}")

    # Check for JSON-RPC error
    if isinstance(body, dict) and "error" in body:
        rpc_err = body["error"]
        issues.append(f"RPC error {rpc_err.get('code')}: {rpc_err.get('message','')}")

    # Check result has content
    if isinstance(body, dict) and "result" in body:
        result = body["result"]
        if isinstance(result, dict):
            content = result.get("content", [])
            if not content:
                issues.append("empty content array in result")

    timing = f"{elapsed_ms}ms"
    if elapsed_ms > timeout_warn_ms:
        timing_str = f"{YELLOW}{timing}{RESET}"
        warn_count += 1
    else:
        timing_str = timing

    if issues:
        print(f"  {RED}✗ FAIL{RESET}  {name} [{timing_str}]")
        for i in issues:
            print(f"         → {i}")
        fail_count += 1
    else:
        print(f"  {GREEN}✓ PASS{RESET}  {name} [{timing_str}]")
        pass_count += 1

def _section(title):
    print(f"\n{BOLD}{CYAN}── {title} {'─' * (50 - len(title))}{RESET}")


# =============================================================================
# TOOL TESTS  (grouped by Neon-direct vs REST-proxy)
# =============================================================================

def test_initialize(url):
    _section("Handshake")
    s, b, ms = _rpc(url, "initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "qa-mcp-test", "version": "1.0"}
    })
    _check("initialize", s, b, ms)
    if isinstance(b, dict):
        r = b.get("result", {})
        sv = r.get("serverInfo", {})
        print(f"         server={sv.get('name','?')}  version={sv.get('version','?')}")

def test_tools_list(url):
    _section("Tool Discovery")
    s, b, ms = _rpc(url, "tools/list")
    _check("tools/list", s, b, ms)
    if isinstance(b, dict) and "result" in b:
        tools = b["result"].get("tools", [])
        names = [t["name"] for t in tools]
        print(f"         {len(names)} tools: {', '.join(names[:8])}{'...' if len(names) > 8 else ''}")
        return names
    return []

def test_neon_direct_tools(url):
    """Tools that query Neon directly (should be fast, <3s)."""
    _section("Neon-Direct Tools (target <3s)")

    cases = [
        ("search_facilities",  {"query": "Dallas", "limit": 3},              5000),
        ("get_facility",       {"facility_id": "equinix-da1"},               5000),
        ("list_transactions",  {"limit": 5},                                  5000),
        ("get_infrastructure", {"state": "TX", "data_type": "substations"},  8000),
        ("get_energy_prices",  {"state": "TX", "data_type": "retail_rates"}, 8000),
        ("get_renewable_energy", {"state": "CA"},                            10000),
        ("get_fiber_intel",    {"state": "VA"},                               8000),
        ("get_agent_registry", {},                                             5000),
        ("get_intelligence_index", {},                                         8000),
    ]

    for tool, args, warn_ms in cases:
        s, b, ms = _tool_call(url, tool, args)
        _check(f"tools/call {tool}", s, b, ms, timeout_warn_ms=warn_ms)

def test_rest_proxy_tools(url):
    """Tools that fall back to REST (acceptable up to 8s)."""
    _section("REST-Proxy / Computed Tools (target <8s)")

    cases = [
        ("get_market_intel",   {"market": "Northern Virginia"},              10000),
        ("get_colocation_score", {"state": "TX", "city": "Dallas"},         10000),
        ("get_grid_data",      {"iso": "ERCOT", "metric": "demand"},        10000),
        ("get_grid_headroom",  {"state": "TX"},                             10000),
        ("get_tax_incentives", {"state": "TX"},                             10000),
        ("get_water_risk",     {"state": "AZ"},                             10000),
        ("get_news",           {"limit": 5},                                 8000),
        ("get_pipeline",       {"limit": 5},                                 8000),
    ]

    for tool, args, warn_ms in cases:
        s, b, ms = _tool_call(url, tool, args)
        _check(f"tools/call {tool}", s, b, ms, timeout_warn_ms=warn_ms)

def test_complex_tools(url):
    """Heavy analysis tools — allowed up to 20s."""
    _section("Analysis Tools (target <20s)")

    cases = [
        ("analyze_site",       {"state": "TX", "city": "Austin", "mw": 50}, 20000),
        ("get_geothermal_potential", {"state": "NV"},                        15000),
        ("get_microgrid_viability",  {"state": "TX", "city": "Dallas"},     15000),
        ("get_dchub_recommendation", {"use_case": "colocation"},            10000),
        ("compare_sites",      {"locations": ["Dallas TX", "Phoenix AZ"]},  20000),
        ("get_grid_intelligence", {"iso": "ERCOT"},                         15000),
        ("get_backup_status",  {},                                            8000),
    ]

    for tool, args, warn_ms in cases:
        s, b, ms = _tool_call(url, tool, args)
        _check(f"tools/call {tool}", s, b, ms, timeout_warn_ms=warn_ms)

def test_quick_subset(url):
    """Fast subset for CI/pre-deploy checks — 5 tools only."""
    _section("Quick Subset (5 tools, CI-friendly)")
    cases = [
        ("search_facilities",  {"query": "Dallas", "limit": 2},  5000),
        ("list_transactions",  {"limit": 3},                      5000),
        ("get_news",           {"limit": 3},                      8000),
        ("get_agent_registry", {},                                 5000),
        ("get_backup_status",  {},                                 8000),
    ]
    for tool, args, warn_ms in cases:
        s, b, ms = _tool_call(url, tool, args)
        _check(f"tools/call {tool}", s, b, ms, timeout_warn_ms=warn_ms)


# =============================================================================
# RUNNER
# =============================================================================

def run(env_name, url, args):
    global pass_count, fail_count, warn_count
    pass_count = fail_count = warn_count = 0

    print(f"\n{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}  DC Hub MCP QA — {env_name.upper()}{RESET}")
    print(f"  Endpoint: {url}")
    print(f"  Time    : {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"{BOLD}{'='*60}{RESET}")

    test_initialize(url)
    tool_names = test_tools_list(url)

    if args.tool:
        # Single tool mode
        _section(f"Single Tool: {args.tool}")
        s, b, ms = _tool_call(url, args.tool, {})
        _check(f"tools/call {args.tool}", s, b, ms)
        if isinstance(b, dict) and "result" in b:
            content = b["result"].get("content", [])
            if content:
                text = content[0].get("text", "")
                print(f"\n{CYAN}Response preview:{RESET}")
                print(text[:800] + ("..." if len(text) > 800 else ""))
    elif args.quick:
        test_quick_subset(url)
    else:
        test_neon_direct_tools(url)
        test_rest_proxy_tools(url)
        test_complex_tools(url)

    print(f"\n{BOLD}── Summary {'─'*48}{RESET}")
    print(f"  {GREEN}PASS: {pass_count}{RESET}   {RED}FAIL: {fail_count}{RESET}   {YELLOW}WARN (slow): {warn_count}{RESET}")
    if fail_count == 0:
        print(f"  {GREEN}{BOLD}✓ MCP tools QA passed for {env_name}{RESET}")
    else:
        print(f"  {RED}{BOLD}✗ {fail_count} tool(s) failed{RESET}")

    return fail_count == 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DC Hub MCP Tools QA")
    parser.add_argument("--env",   default="railway", choices=["railway","replit","local","both"])
    parser.add_argument("--tool",  default="",  help="Test a single tool by name")
    parser.add_argument("--quick", action="store_true", help="Run 5-tool quick subset only")
    args = parser.parse_args()

    envs = ["railway","replit"] if args.env == "both" else [args.env]
    all_ok = True
    for env in envs:
        ok = run(env, TARGETS[env], args)
        all_ok = all_ok and ok

    sys.exit(0 if all_ok else 1)
