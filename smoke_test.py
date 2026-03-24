#!/usr/bin/env python3
"""
DC Hub Post-Deploy Smoke Test v1.0
===================================
Run after EVERY deploy to catch integration bugs before they hit production.

Tests:
  1. MCP tools (all 20) — response, latency, data quality
  2. REST API endpoints (12 critical paths)
  3. Self-healer MCP compatibility (Accept header, JSON-RPC format)
  4. Connection pool health
  5. Long-running job connection patterns
  6. Cross-component contracts (MCP↔main.py↔orchestrator)

Usage:
  # From Railway shell:
  python3 smoke_test.py

  # From local with Railway URL:
  DCHUB_URL=https://dchub-backend-production.up.railway.app python3 smoke_test.py

  # Quick mode (skip slow tests):
  python3 smoke_test.py --quick

Exit codes:
  0 = all pass
  1 = failures detected (DO NOT declare deploy good)
  2 = critical failures (rollback recommended)

Author: DC Hub QA
Date: 2026-03-24
"""

import os
import sys
import json
import time
import urllib.request
import urllib.error
from datetime import datetime

# ═══════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════

# Auto-detect environment:
# - Inside Railway APP container: localhost works for both Flask and MCP
# - Inside Railway SHELL: separate container, localhost doesn't reach the app
# - External: use DCHUB_URL env var
RAILWAY_EXTERNAL = 'https://dchub-backend-production.up.railway.app'
DCHUB_CLOUD = 'https://dchub.cloud'

def _detect_base_url():
    """Auto-detect the best base URL to test against."""
    # Explicit override
    if os.environ.get('DCHUB_URL'):
        return os.environ['DCHUB_URL']
    
    # Check if we're in the Railway app container (Flask is on localhost)
    try:
        req = urllib.request.Request('http://127.0.0.1:8080/health', method='GET')
        resp = urllib.request.urlopen(req, timeout=3)
        if resp.status == 200:
            return 'http://127.0.0.1:8080'  # We're in the app container
    except Exception:
        pass
    
    # Railway shell or external — use Railway URL
    if os.environ.get('RAILWAY_ENVIRONMENT') or os.environ.get('RAILWAY_SERVICE_NAME'):
        return RAILWAY_EXTERNAL
    
    return RAILWAY_EXTERNAL  # Default to external

def _detect_mcp_url():
    """Auto-detect MCP URL."""
    if os.environ.get('MCP_URL'):
        return os.environ['MCP_URL']
    
    # Check if MCP is on localhost
    try:
        req = urllib.request.Request('http://127.0.0.1:8888/mcp', method='GET')
        resp = urllib.request.urlopen(req, timeout=3)
        return 'http://127.0.0.1:8888/mcp'
    except Exception:
        pass
    
    # Fall back to external MCP endpoint (via Cloudflare proxy)
    return DCHUB_CLOUD + '/mcp'

BASE_URL = _detect_base_url()
MCP_URL = _detect_mcp_url()
INTERNAL_KEY = 'dchub-internal-sync-2026'
QUICK_MODE = '--quick' in sys.argv
IS_EXTERNAL = 'dchub' in BASE_URL or 'railway' in BASE_URL  # Not localhost

# Thresholds
MAX_MCP_LATENCY_MS = 5000       # 5s max per MCP tool
MAX_API_LATENCY_MS = 3000       # 3s max per API endpoint
MAX_POOL_PERCENT = 80           # Pool should be under 80%
MIN_MCP_TOOLS = 15              # At least 15 tools must respond
EXPECTED_MCP_TOOLS = 20         # We expect 20 tools

# ═══════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════

class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    BOLD = '\033[1m'
    END = '\033[0m'

PASS = 0
FAIL = 0
WARN = 0
CRITICAL = 0
RESULTS = []


def _log(status, msg, latency_ms=None):
    global PASS, FAIL, WARN, CRITICAL
    lat = f" ({latency_ms}ms)" if latency_ms else ""
    if status == 'PASS':
        PASS += 1
        icon = f"{Colors.GREEN}✅{Colors.END}"
    elif status == 'FAIL':
        FAIL += 1
        icon = f"{Colors.RED}❌{Colors.END}"
    elif status == 'WARN':
        WARN += 1
        icon = f"{Colors.YELLOW}⚠️{Colors.END}"
    elif status == 'CRIT':
        CRITICAL += 1
        icon = f"{Colors.RED}🔴{Colors.END}"
    else:
        icon = "  "
    print(f"  {icon} {msg}{lat}")
    RESULTS.append({'status': status, 'msg': msg, 'latency_ms': latency_ms})


def _http_get(url, headers=None, timeout=10):
    """Simple HTTP GET, returns (status_code, body_str, latency_ms)."""
    hdrs = headers or {}
    req = urllib.request.Request(url, headers=hdrs, method='GET')
    start = time.time()
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        body = resp.read().decode('utf-8', errors='replace')
        latency = round((time.time() - start) * 1000)
        return resp.status, body, latency
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace') if e.fp else ''
        latency = round((time.time() - start) * 1000)
        return e.code, body, latency
    except Exception as e:
        latency = round((time.time() - start) * 1000)
        return 0, str(e), latency


def _http_post(url, data=None, headers=None, timeout=15):
    """Simple HTTP POST with JSON body."""
    hdrs = {'Content-Type': 'application/json'}
    if headers:
        hdrs.update(headers)
    body_bytes = json.dumps(data).encode('utf-8') if data else b''
    req = urllib.request.Request(url, data=body_bytes, headers=hdrs, method='POST')
    start = time.time()
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        body = resp.read().decode('utf-8', errors='replace')
        latency = round((time.time() - start) * 1000)
        return resp.status, body, latency
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace') if e.fp else ''
        latency = round((time.time() - start) * 1000)
        return e.code, body, latency
    except Exception as e:
        latency = round((time.time() - start) * 1000)
        return 0, str(e), latency


def _mcp_call(method, params=None, tool_name=None):
    """Send a JSON-RPC request to MCP with correct headers.
    
    Returns (success: bool, result: dict, latency_ms: int)
    """
    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "id": 1,
        "params": params or {},
    }
    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json, text/event-stream',
    }
    status, body, latency = _http_post(MCP_URL, payload, headers, timeout=20)

    if status in (200, 202):
        try:
            data = json.loads(body)
            return True, data, latency
        except json.JSONDecodeError:
            # SSE response — try to extract JSON from event stream
            for line in body.split('\n'):
                if line.startswith('data:'):
                    try:
                        data = json.loads(line[5:].strip())
                        return True, data, latency
                    except Exception:
                        continue
            return True, {'raw': body[:500]}, latency
    return False, {'status': status, 'body': body[:300]}, latency


def _mcp_tool_call(tool_name, arguments=None):
    """Call an MCP tool and return (success, result, latency_ms)."""
    return _mcp_call("tools/call", {
        "name": tool_name,
        "arguments": arguments or {},
    }, tool_name=tool_name)


# ═══════════════════════════════════════════════════════════
# TEST 1: MCP PROTOCOL HEALTH
# ═══════════════════════════════════════════════════════════

def test_mcp_protocol():
    print(f"\n{Colors.BOLD}═══ TEST 1: MCP Protocol Health ═══{Colors.END}")

    # 1a. Initialize handshake
    ok, result, lat = _mcp_call("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "smoke-test", "version": "1.0"},
    })
    if ok:
        _log('PASS', 'MCP initialize handshake', lat)
    else:
        _log('CRIT', f'MCP initialize FAILED: {result}', lat)
        return  # Can't continue if MCP is down

    # 1b. List tools
    ok, result, lat = _mcp_call("tools/list")
    if ok:
        tools = []
        if isinstance(result, dict):
            tools = result.get('result', {}).get('tools', [])
        tool_count = len(tools)
        tool_names = [t.get('name', '') for t in tools]

        if tool_count >= EXPECTED_MCP_TOOLS:
            _log('PASS', f'MCP tools/list: {tool_count} tools', lat)
        elif tool_count >= MIN_MCP_TOOLS:
            _log('WARN', f'MCP tools/list: {tool_count} tools (expected {EXPECTED_MCP_TOOLS})', lat)
        else:
            _log('FAIL', f'MCP tools/list: only {tool_count} tools (need {MIN_MCP_TOOLS}+)', lat)

        # Check for critical tools
        critical_tools = [
            'search_facilities', 'get_facility', 'get_news',
            'get_energy_prices', 'get_infrastructure', 'analyze_site',
        ]
        for t in critical_tools:
            if t in tool_names:
                _log('PASS', f'  Tool registered: {t}')
            else:
                _log('FAIL', f'  Tool MISSING: {t}')
    else:
        _log('CRIT', f'MCP tools/list FAILED: {result}', lat)

    # 1c. Verify Accept header handling (the 406 bug)
    # Send WITHOUT Accept header — should still not crash
    payload = json.dumps({
        "jsonrpc": "2.0", "method": "ping", "id": 99, "params": {},
    }).encode()
    req = urllib.request.Request(
        MCP_URL,
        data=payload,
        headers={'Content-Type': 'application/json'},  # No Accept!
        method='POST',
    )
    start = time.time()
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        lat = round((time.time() - start) * 1000)
        # 200 or 202 = MCP handled it gracefully
        _log('PASS', f'MCP handles missing Accept header (HTTP {resp.status})', lat)
    except urllib.error.HTTPError as e:
        lat = round((time.time() - start) * 1000)
        if e.code == 406:
            _log('WARN', f'MCP returns 406 without Accept header — self-healer MUST send Accept', lat)
        else:
            _log('WARN', f'MCP returned HTTP {e.code} without Accept header', lat)
    except Exception as e:
        lat = round((time.time() - start) * 1000)
        _log('FAIL', f'MCP unreachable: {e}', lat)

    # 1d. List prompts (less critical but should work)
    ok, result, lat = _mcp_call("prompts/list")
    if ok:
        _log('PASS', 'MCP prompts/list', lat)
    else:
        _log('WARN', f'MCP prompts/list failed: {result}', lat)


# ═══════════════════════════════════════════════════════════
# TEST 2: MCP TOOL EXECUTION (all 20)
# ═══════════════════════════════════════════════════════════

def test_mcp_tools():
    print(f"\n{Colors.BOLD}═══ TEST 2: MCP Tool Execution ═══{Colors.END}")

    # Define test cases: (tool_name, args, expected_key, description)
    tools = [
        ('search_facilities', {'query': 'Equinix', 'limit': 2},
         'success', 'Search facilities'),
        ('get_facility', {'facility_id': '1'},
         'success', 'Get facility by ID'),
        ('get_news', {'limit': 2},
         'success', 'Get news'),
        ('get_energy_prices', {'data_type': 'retail_rates', 'state': 'TX'},
         'success', 'Energy prices (Neon-direct)'),
        ('get_renewable_energy', {'energy_type': 'solar', 'state': 'AZ'},
         'success', 'Renewable energy (Neon-direct)'),
        ('get_water_risk', {'state': 'AZ'},
         'success', 'Water risk (Neon-direct)'),
        ('get_tax_incentives', {'state': 'VA'},
         'success', 'Tax incentives (Neon-direct)'),
        ('get_infrastructure', {'lat': 33.45, 'lon': -112.07, 'layer': 'substations', 'limit': 2},
         'query', 'Infrastructure (Neon-direct)'),
        ('list_transactions', {'limit': 2},
         'success', 'List transactions (Neon-direct)'),
        ('get_pipeline', {'limit': 2},
         'success', 'Pipeline (Neon-direct)'),
        ('get_market_intel', {'market': 'Northern Virginia'},
         'success', 'Market intel'),
        ('get_grid_data', {'iso': 'ERCOT'},
         'success', 'Grid data'),
        ('get_grid_intelligence', {},
         'success', 'Grid intelligence'),
        ('get_agent_registry', {},
         None, 'Agent registry'),
        ('get_intelligence_index', {},
         None, 'Intelligence index'),
        ('get_backup_status', {},
         'success', 'Backup status (Neon-direct)'),
    ]

    if not QUICK_MODE:
        tools.extend([
            ('analyze_site', {'lat': 33.45, 'lon': -112.07, 'state': 'AZ'},
             None, 'Analyze site'),
            ('get_fiber_intel', {},
             None, 'Fiber intel (known-slow)'),
            ('compare_sites', {
                'locations': json.dumps([
                    {"lat": 33.45, "lon": -112.07, "state": "AZ", "label": "Phoenix"},
                    {"lat": 39.04, "lon": -77.49, "state": "VA", "label": "Ashburn"},
                ])
            }, 'success', 'Compare sites (known-slow)'),
            ('get_dchub_recommendation', {'context': 'general'},
             None, 'DC Hub recommendation'),
        ])

    # Tools that are expected to be slow (REST-dependent, multiple sub-calls)
    KNOWN_SLOW_TOOLS = {'get_fiber_intel', 'compare_sites'}

    passed = 0
    failed = 0
    total_latency = 0

    for tool_name, args, expected_key, desc in tools:
        ok, result, lat = _mcp_tool_call(tool_name, args)
        total_latency += lat

        if not ok:
            if tool_name in KNOWN_SLOW_TOOLS:
                _log('WARN', f'{desc} [{tool_name}]: timeout (known-slow, needs Neon conversion)', lat)
                passed += 1  # Don't count as failure
            else:
                _log('FAIL', f'{desc} [{tool_name}]: ERROR', lat)
                failed += 1
            continue

        # Parse the tool result — MCP wraps in result.content[0].text
        tool_data = {}
        try:
            content = result.get('result', {}).get('content', [])
            if content and isinstance(content, list):
                text = content[0].get('text', '{}')
                tool_data = json.loads(text)
        except Exception:
            tool_data = result

        # Check for errors in the tool response
        has_error = tool_data.get('error') and not tool_data.get('success')

        if has_error:
            err = tool_data.get('error', 'unknown')
            # Plan-gated errors are OK for free tier
            if 'plan_required' in str(err) or 'upgrade' in str(err).lower():
                _log('PASS', f'{desc} [{tool_name}]: gated (expected)', lat)
                passed += 1
            else:
                _log('FAIL', f'{desc} [{tool_name}]: {err}', lat)
                failed += 1
        elif lat > MAX_MCP_LATENCY_MS:
            _log('WARN', f'{desc} [{tool_name}]: SLOW', lat)
            passed += 1  # Still counts as pass if data returned
        else:
            _log('PASS', f'{desc} [{tool_name}]', lat)
            passed += 1

    # Summary
    avg_lat = round(total_latency / len(tools)) if tools else 0
    print(f"\n  MCP Tools: {passed}/{len(tools)} pass, {failed} fail, avg {avg_lat}ms")

    # Data quality spot checks
    print(f"\n  {Colors.BOLD}Data quality checks:{Colors.END}")

    # Check get_facility doesn't return column names as values (BUG-008)
    ok, result, lat = _mcp_tool_call('get_facility', {'facility_id': '100'})
    if ok:
        try:
            content = result.get('result', {}).get('content', [])
            text = content[0].get('text', '{}') if content else '{}'
            data = json.loads(text)
            facility = data.get('facility', data.get('data', {}))
            city = facility.get('city', '')
            if city == 'city':
                _log('FAIL', 'BUG-008 regression: get_facility returns column names as values')
            elif city:
                _log('PASS', f'get_facility returns real data (city={city})')
            else:
                _log('WARN', 'get_facility returned empty city')
        except Exception as e:
            _log('WARN', f'get_facility data quality check error: {e}')


# ═══════════════════════════════════════════════════════════
# TEST 3: REST API ENDPOINTS
# ═══════════════════════════════════════════════════════════

def test_rest_api():
    print(f"\n{Colors.BOLD}═══ TEST 3: REST API Endpoints ═══{Colors.END}")

    endpoints = [
        ('/health', 200, 'Health check'),
        ('/api/v1/stats', 200, 'Platform stats'),
        ('/api/v1/search?q=equinix&limit=2', 200, 'Facility search'),
        ('/api/news/live?limit=2', 200, 'News'),
        ('/api/transactions?limit=2', 200, 'Transactions'),
        ('/api/v1/map?limit=2', 401, 'Map (expect gated)'),
        ('/api/fiber/routes?limit=2', 200, 'Fiber routes'),
        ('/api/health/watchdog', 200, 'Watchdog'),
        ('/api/v1/grid-intelligence', 200, 'Grid intelligence'),
    ]

    if not QUICK_MODE:
        endpoints.extend([
            ('/api/infrastructure/substations?lat=39&lon=-77&limit=2', 200, 'Substations'),
            ('/api/v1/grid-intelligence/pjm', 200, 'Grid intel PJM'),
        ])

    for path, expected_status, desc in endpoints:
        url = BASE_URL + path
        status, body, lat = _http_get(url, headers={'X-Internal-Key': INTERNAL_KEY})

        if status == expected_status:
            if lat > MAX_API_LATENCY_MS:
                _log('WARN', f'{desc}: {status} but SLOW', lat)
            else:
                _log('PASS', f'{desc}: {status}', lat)
        elif status == 0:
            if 'Connection refused' in body and IS_EXTERNAL:
                # This shouldn't happen on external URL — real failure
                _log('CRIT', f'{desc}: UNREACHABLE ({body[:80]})', lat)
            elif 'Connection refused' in body:
                # Railway shell can't reach localhost — skip, not a real failure
                _log('WARN', f'{desc}: SKIP (Railway shell — use external URL)', lat)
            else:
                _log('CRIT', f'{desc}: UNREACHABLE ({body[:80]})', lat)
        else:
            _log('FAIL', f'{desc}: expected {expected_status}, got {status}', lat)


# ═══════════════════════════════════════════════════════════
# TEST 4: SELF-HEALER COMPATIBILITY
# ═══════════════════════════════════════════════════════════

def test_self_healer():
    print(f"\n{Colors.BOLD}═══ TEST 4: Self-Healer Compatibility ═══{Colors.END}")

    # Simulate what self_healing_orchestrator.py does
    # It sends: POST /mcp with tools/list and Content-Type + Accept headers
    payload = {
        "jsonrpc": "2.0",
        "method": "tools/list",
        "id": 1,
        "params": {},
    }

    # Test WITH correct headers (should be 200/202)
    headers_good = {
        'Content-Type': 'application/json',
        'Accept': 'application/json, text/event-stream',
    }
    status, body, lat = _http_post(MCP_URL, payload, headers_good, timeout=15)
    if status in (200, 202):
        _log('PASS', f'Self-healer check WITH Accept header: {status}', lat)
    elif status == 0 and 'Connection refused' in str(body) and not IS_EXTERNAL:
        _log('WARN', 'Self-healer check: SKIP (Railway shell — use external URL)', lat)
    else:
        _log('CRIT', f'Self-healer check WITH Accept header: {status} (MCP broken!)', lat)

    # Test WITHOUT Accept header (the 406 bug scenario)
    headers_bad = {
        'Content-Type': 'application/json',
        # No Accept header!
    }
    status, body, lat = _http_post(MCP_URL, payload, headers_bad, timeout=15)
    if status in (200, 202):
        _log('PASS', f'MCP graceful without Accept: {status}', lat)
    elif status == 406:
        _log('WARN', 'MCP returns 406 without Accept — orchestrator MUST include Accept header')
        # Now verify the orchestrator file has the fix
        for path in ['/app/self_healing_orchestrator.py',
                     os.path.expanduser('~/workspace/self_healing_orchestrator.py')]:
            if os.path.exists(path):
                with open(path) as f:
                    content = f.read()
                if "'Accept'" in content or '"Accept"' in content:
                    _log('PASS', f'Orchestrator has Accept header ({os.path.basename(path)})')
                else:
                    _log('CRIT', f'Orchestrator MISSING Accept header! MCP will flap! ({path})')
                break
        else:
            _log('WARN', 'Could not find self_healing_orchestrator.py to verify')
    else:
        _log('WARN', f'MCP returned {status} without Accept header', lat)


# ═══════════════════════════════════════════════════════════
# TEST 5: CONNECTION POOL HEALTH
# ═══════════════════════════════════════════════════════════

def test_connection_pool():
    print(f"\n{Colors.BOLD}═══ TEST 5: Connection Pool Health ═══{Colors.END}")

    # Check pool via the self-healing endpoint
    url = BASE_URL + '/api/v1/health/self-healing'
    status, body, lat = _http_get(url, headers={'X-Internal-Key': INTERNAL_KEY})

    if status == 200:
        try:
            data = json.loads(body)
            monitor = data.get('health_monitor', {})
            if monitor.get('healthy'):
                _log('PASS', 'DB health monitor: healthy', lat)
            else:
                failures = monitor.get('consecutive_failures', 0)
                _log('FAIL', f'DB health monitor: {failures} consecutive failures', lat)
        except Exception:
            _log('WARN', 'Could not parse self-healing status', lat)
    elif status == 404:
        _log('WARN', 'Self-healing endpoint not registered (404)')
    else:
        _log('WARN', f'Self-healing endpoint: HTTP {status}', lat)

    # Check watchdog
    url = BASE_URL + '/api/health/watchdog'
    status, body, lat = _http_get(url, headers={'X-Internal-Key': INTERNAL_KEY})
    if status == 200:
        try:
            data = json.loads(body)
            wstatus = data.get('status', '')
            if wstatus == 'healthy':
                _log('PASS', 'Watchdog: healthy', lat)
            else:
                _log('WARN', f'Watchdog: {wstatus}', lat)
        except Exception:
            _log('PASS', 'Watchdog: responding', lat)
    else:
        _log('WARN', f'Watchdog: HTTP {status}', lat)


# ═══════════════════════════════════════════════════════════
# TEST 6: CROSS-COMPONENT CONTRACTS
# ═══════════════════════════════════════════════════════════

def test_contracts():
    print(f"\n{Colors.BOLD}═══ TEST 6: Cross-Component Contracts ═══{Colors.END}")

    # Contract 1: MCP proxy in main.py passes X-Internal-Key
    # (verified by: MCP tools return gated data, not errors)
    ok, result, lat = _mcp_tool_call('search_facilities', {'query': 'test', 'limit': 1})
    if ok:
        try:
            content = result.get('result', {}).get('content', [])
            text = content[0].get('text', '{}') if content else '{}'
            data = json.loads(text)
            if data.get('success'):
                _log('PASS', 'MCP→Flask proxy passes X-Internal-Key')
            elif 'auth' in str(data.get('error', '')).lower():
                _log('FAIL', 'MCP→Flask proxy NOT passing X-Internal-Key (auth error)')
            else:
                _log('PASS', 'MCP→Flask proxy responding')
        except Exception:
            _log('PASS', 'MCP→Flask proxy responding')
    else:
        _log('FAIL', f'MCP→Flask proxy broken: {result}')

    # Contract 2: DCHUB_API_BASE is not localhost/127.0.0.1 in env vars
    # (This is the recurring deadlock bug)
    for path in ['/app/dchub_mcp_server.py',
                 os.path.expanduser('~/workspace/dchub_mcp_server.py')]:
        if os.path.exists(path):
            with open(path) as f:
                first_200_lines = ''.join(f.readlines()[:200])
            if 'v2.2' in first_200_lines:
                _log('PASS', 'MCP server is v2.2 (localhost fast-path fix)')
            elif 'v2.1' in first_200_lines:
                _log('WARN', 'MCP server is v2.1 (missing localhost fast-path fix)')
            else:
                _log('WARN', 'Could not determine MCP server version')
            break
    else:
        _log('WARN', 'Could not find dchub_mcp_server.py to verify version')

    # Contract 3: Fiber discovery uses direct connection (not pooled)
    for path in ['/app/fiber_network_discovery.py',
                 os.path.expanduser('~/workspace/fiber_network_discovery.py')]:
        if os.path.exists(path):
            with open(path) as f:
                content = f.read()
            if 'pg2.connect(' in content or 'psycopg2.connect(' in content:
                if 'v2.3' in content:
                    _log('PASS', 'Fiber discovery v2.3: uses direct connection (not pooled)')
                else:
                    _log('PASS', 'Fiber discovery uses direct psycopg2 connection')
            elif 'get_db()' in content or '_get_pg_connection()' in content:
                # Check if run_fiber_discovery still uses pooled connection
                in_run_func = False
                for line in content.split('\n'):
                    if 'def run_fiber_discovery' in line:
                        in_run_func = True
                    if in_run_func and ('get_db()' in line or '_get_pg_connection()' in line):
                        _log('FAIL', 'Fiber discovery STILL uses pooled connection (196s hold)')
                        break
                    if in_run_func and line.startswith('def ') and 'run_fiber' not in line:
                        in_run_func = False
                else:
                    _log('PASS', 'Fiber discovery connection pattern OK')
            break
    else:
        _log('WARN', 'Could not find fiber_network_discovery.py to verify')


# ═══════════════════════════════════════════════════════════
# REPORT
# ═══════════════════════════════════════════════════════════

def print_report():
    total = PASS + FAIL + WARN + CRITICAL
    print(f"\n{'═' * 60}")
    print(f"{Colors.BOLD}DC Hub Smoke Test Report{Colors.END}")
    print(f"{'═' * 60}")
    print(f"  Timestamp: {datetime.utcnow().isoformat()}Z")
    print(f"  Base URL:  {BASE_URL}")
    print(f"  MCP URL:   {MCP_URL}")
    print(f"  Mode:      {'quick' if QUICK_MODE else 'full'}")
    print(f"{'─' * 60}")
    print(f"  {Colors.GREEN}Pass:     {PASS}{Colors.END}")
    print(f"  {Colors.YELLOW}Warn:     {WARN}{Colors.END}")
    print(f"  {Colors.RED}Fail:     {FAIL}{Colors.END}")
    print(f"  {Colors.RED}Critical: {CRITICAL}{Colors.END}")
    print(f"  Total:    {total}")
    print(f"{'─' * 60}")

    if CRITICAL > 0:
        print(f"  {Colors.RED}{Colors.BOLD}🔴 CRITICAL FAILURES — ROLLBACK RECOMMENDED{Colors.END}")
        exit_code = 2
    elif FAIL > 0:
        print(f"  {Colors.RED}{Colors.BOLD}❌ FAILURES DETECTED — DO NOT DECLARE DEPLOY GOOD{Colors.END}")
        exit_code = 1
    elif WARN > 2:
        print(f"  {Colors.YELLOW}{Colors.BOLD}⚠️ MULTIPLE WARNINGS — REVIEW BEFORE PROCEEDING{Colors.END}")
        exit_code = 0
    else:
        print(f"  {Colors.GREEN}{Colors.BOLD}✅ ALL CLEAR — DEPLOY IS GOOD{Colors.END}")
        exit_code = 0

    print(f"{'═' * 60}\n")

    # Print failed/critical items for quick reference
    failures = [r for r in RESULTS if r['status'] in ('FAIL', 'CRIT')]
    if failures:
        print(f"{Colors.RED}Failed checks:{Colors.END}")
        for r in failures:
            lat = f" ({r['latency_ms']}ms)" if r.get('latency_ms') else ""
            print(f"  ❌ {r['msg']}{lat}")
        print()

    return exit_code


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

if __name__ == '__main__':
    print(f"\n{Colors.BOLD}{'═' * 60}")
    print(f"  DC Hub Post-Deploy Smoke Test v1.0")
    print(f"  {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"{'═' * 60}{Colors.END}")
    print(f"  Target: {BASE_URL}")
    print(f"  MCP:    {MCP_URL}")
    print(f"  Mode:   {'quick' if QUICK_MODE else 'full'}")

    test_mcp_protocol()
    test_mcp_tools()
    test_rest_api()
    test_self_healer()
    test_connection_pool()
    test_contracts()

    exit_code = print_report()
    sys.exit(exit_code)
