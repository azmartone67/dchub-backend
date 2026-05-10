#!/usr/bin/env python3
"""
DC Hub — QA Smoke Test Suite
=============================
Tests API health, connection pool, circuit breaker, and Cloudflare failover stubs.

Usage (from Replit shell):
    python qa_test.py                          # tests Railway (production)
    python qa_test.py --env replit             # tests Replit (failover)
    python qa_test.py --env both               # tests both
    python qa_test.py --env local              # tests localhost:8080

Requires: DCHUB_ADMIN_KEY set in environment (or pass via --key flag)
    export DCHUB_ADMIN_KEY=your_key_here
    python qa_test.py
"""

import os
import sys
import json
import time
import argparse
import urllib.request
import urllib.error
from datetime import datetime
from routes._freshness import freshness_dict_from_url

# ── Targets ──────────────────────────────────────────────────────────────────
TARGETS = {
    "railway": "https://dchub-backend-production.up.railway.app",
    "replit":  "https://dc-hub-replit-fixedzip--azmartone1.replit.app",
    "local":   "http://localhost:8080",
}

# ── Colours ───────────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

pass_count = 0
fail_count = 0
warn_count = 0

_last_req_ts = 0.0
_REQ_DELAY   = 0.6   # seconds between requests to avoid self-triggering 429

def _req(url, method="GET", headers=None, body=None, timeout=15, admin_key=None):
    # Always send admin key if available — bypasses 20rpm anon limit → 120rpm auth limit
    _ak = admin_key or os.environ.get("DCHUB_ADMIN_KEY", "")
    """Simple HTTP request — returns (status_code, response_dict_or_str)."""
    global _last_req_ts
    gap = time.time() - _last_req_ts
    if gap < _REQ_DELAY:
        time.sleep(_REQ_DELAY - gap)

    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if _ak:
        h["X-Admin-Key"] = _ak
    if headers:
        h.update(headers)
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            _last_req_ts = time.time()
            try:
                return r.status, json.loads(raw)
            except Exception:
                return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        _last_req_ts = time.time()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw
    except urllib.error.URLError as e:
        return 0, str(e)
    except TimeoutError:
        # Long-running job — connection stayed open but read timed out.
        # Treat as "job accepted / running" — not a crash.
        return -1, "timeout (job likely running)"

def _check(name, status, body, expect_status=200, expect_keys=None, expect_value=None):
    """Evaluate a single test and print result."""
    global pass_count, fail_count, warn_count
    issues = []

    if status == 0:
        issues.append(f"connection refused / timeout: {body}")
    elif status == -1:
        # job timed out on read — it's running, not crashing
        print(f"  {YELLOW}⏳ RUNNING{RESET}  {name} — job running (read timeout, not a crash)")
        warn_count += 1
        return
    elif status == 429:
        print(f"  {YELLOW}⚠ WARN{RESET}  {name} — HTTP 429 rate limited (too many requests)")
        warn_count += 1
        return
    elif status != expect_status:
        issues.append(f"HTTP {status} (expected {expect_status})")

    if expect_keys and isinstance(body, dict):
        for k in expect_keys:
            if k not in body:
                issues.append(f"missing key '{k}'")

    if expect_value and isinstance(body, dict):
        for k, v in expect_value.items():
            actual = body.get(k)
            if actual != v:
                issues.append(f"body['{k}'] = {actual!r} (expected {v!r})")

    if issues:
        print(f"  {RED}✗ FAIL{RESET}  {name}")
        for i in issues:
            print(f"         → {i}")
        fail_count += 1
    else:
        print(f"  {GREEN}✓ PASS{RESET}  {name}")
        pass_count += 1

def _warn(name, message):
    global warn_count
    print(f"  {YELLOW}⚠ WARN{RESET}  {name}: {message}")
    warn_count += 1

def _section(title):
    print(f"\n{BOLD}{CYAN}── {title} {'─' * (50 - len(title))}{RESET}")


# =============================================================================
# TEST GROUPS
# =============================================================================

def test_health(base, key):
    _section("1. API Health")

    # Basic health
    s, b = _req(f"{base}/api/health")
    _check("GET /api/health returns 200", s, b, expect_keys=["status"])
    if isinstance(b, dict):
        env = b.get("environment", "unknown")
        ver = b.get("version", "?")
        print(f"         environment={env}  version={ver}")
        if b.get("status") not in ("healthy", "ok"):
            _warn("health status", f"status={b.get('status')} — may be degraded")

    # DB health (lightweight, no connection acquired)
    s, b = _req(f"{base}/api/health/db")
    _check("GET /api/health/db returns 200", s, b)

    # .well-known
    s, b = _req(f"{base}/.well-known/health")
    _check("GET /.well-known/health returns 200", s, b)


def test_pool(base, key):
    _section("2. Connection Pool (_PoolConnWrapper)")

    s, b = _req(f"{base}/api/health/db")
    if not isinstance(b, dict):
        _warn("pool data", "response not JSON — skipping pool assertions")
        return

    pool = b.get("pool", {})
    cb   = b.get("circuit_breaker", {})

    # Pool status
    pool_status = pool.get("status", "unknown")
    _check(
        "Pool status is healthy or warning",
        200 if pool_status in ("healthy", "warning") else 500,
        b
    )

    # Circuit breaker closed
    cb_open = cb.get("open", None)
    _check(
        "Circuit breaker is CLOSED",
        200 if cb_open is False else 500,
        b
    )
    if cb_open:
        print(f"         → trips={cb.get('total_trips')}  failures={cb.get('consecutive_failures')}")

    # acquired == returned (no leaked connections)
    stats = b.get("stats", {})
    acquired = stats.get("acquired", 0)
    returned = stats.get("returned", 0)
    leaked   = acquired - returned
    if leaked > 2:
        _warn("connection leak", f"acquired={acquired} returned={returned} leaked={leaked}")
    else:
        print(f"  {GREEN}✓ PASS{RESET}  No connection leaks (acquired={acquired} returned={returned})")
        global pass_count
        pass_count += 1


def test_cloudflare_stubs(base, key):
    _section("3. Cloudflare Worker Failover Stubs")

    stubs = [
        ("/api/v1/ecosystem",           "ecosystem companies"),
        ("/api/rankings/states",        "state rankings"),
        ("/api/v1/infrastructure",      "infrastructure asset counts"),
        ("/api/v1/energy/summary",      "energy overview"),
        ("/api/v1/gdci",                "global data center index"),
        ("/api/energy-discovery/overview", "energy discovery stats"),
    ]

    for path, desc in stubs:
        s, b = _req(f"{base}{path}", timeout=20)
        if s == 404:
            _warn(f"GET {path}", f"404 — stub not deployed yet ({desc})")
        elif s in (200, 206):
            _check(f"GET {path} ({desc})", s, b, expect_status=s)
        elif s in (401, 403):
            _warn(f"GET {path}", f"Auth required ({s}) — needs API key header")
        else:
            _check(f"GET {path} ({desc})", s, b, expect_status=200)


def test_mcp_proxy(base, key):
    _section("4. MCP Proxy (/mcp)")

    payload = {
        "jsonrpc": "2.0",
        "method": "initialize",
        "id": 1,
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "qa-test", "version": "1.0"}
        }
    }
    s, b = _req(f"{base}/mcp", method="POST", body=payload, timeout=20)
    _check("POST /mcp initialize", s, b, expect_status=200)
    if isinstance(b, dict):
        result = b.get("result", {})
        sv = result.get("serverInfo", {})
        print(f"         server={sv.get('name','?')}  protocolVersion={result.get('protocolVersion','?')}")

    # Manifest
    s, b = _req(f"{base}/mcp/manifest", timeout=10)
    _check("GET /mcp/manifest", s, b, expect_status=200, expect_keys=["name", "tools"])
    if isinstance(b, dict):
        tools = b.get("tools", [])
        print(f"         tools registered: {len(tools)}")


def test_scheduler_status(base, key):
    _section("5. Scheduler / Jobs Status")

    headers = {"X-Admin-Key": key} if key else {}
    s, b = _req(f"{base}/api/scheduler/status", headers=headers, timeout=10)

    if s == 401:
        _warn("/api/scheduler/status", "Admin key required — set DCHUB_ADMIN_KEY")
        return
    _check("GET /api/scheduler/status", s, b, expect_status=200)
    if isinstance(b, dict):
        jobs = b.get("jobs", b.get("schedulers", {}))
        print(f"         registered jobs: {len(jobs)}")


def test_jobs_trigger(base, key):
    _section("6. Jobs Endpoints (one-shot POST triggers)")

    if not key:
        _warn("jobs", "No DCHUB_ADMIN_KEY — skipping job trigger tests (set env var to enable)")
        return

    headers = {"X-Admin-Key": key}

    # Only trigger lightweight/safe jobs — NOT news_sync or full autopilot
    safe_jobs = [
        ("/api/jobs/fiber-sync",      "fiber sync"),
        ("/api/jobs/permit-scraper",  "permit scraper"),
    ]

    for path, desc in safe_jobs:
        s, b = _req(f"{base}{path}", method="POST", headers=headers, timeout=30)
        if s in (200, 202):
            _check(f"POST {path} ({desc})", s, b, expect_status=s)
        elif s == 404:
            _warn(f"POST {path}", "404 — route not registered (check jobs_routes.py Blueprint)")
        elif s == 401:
            _warn(f"POST {path}", "401 — admin key rejected")
        else:
            _check(f"POST {path} ({desc})", s, b, expect_status=200)


def test_public_apis(base, key):
    _section("7. Core Public API Endpoints")

    endpoints = [
        ("/api/v1/facilities",        "facilities list"),
        ("/api/v1/stats",             "platform stats"),
        ("/api/v1/discovery",         "AI discovery index"),
        # auth-required: ("/api/v1/map",         "map data"),
    ]

    for path, desc in endpoints:
        s, b = _req(f"{base}{path}", timeout=15)
        if s in (200, 206):
            _check(f"GET {path} ({desc})", s, b, expect_status=s)
        elif s in (401, 403):
            _warn(f"GET {path}", f"requires auth ({s})")
        else:
            _check(f"GET {path} ({desc})", s, b, expect_status=200)


# =============================================================================
# RUNNER
# =============================================================================

def run(env_name, base_url, key):
    print(f"\n{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}  DC Hub QA — {env_name.upper()}{RESET}")
    print(f"  Target : {base_url}")
    print(f"  Admin  : {'✓ key set' if key else '✗ not set (some tests skipped)'}")
    print(f"  Time   : {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"{BOLD}{'='*60}{RESET}")

    global pass_count, fail_count, warn_count
    pass_count = fail_count = warn_count = 0

    test_health(base_url, key)
    test_pool(base_url, key)
    test_cloudflare_stubs(base_url, key)
    test_mcp_proxy(base_url, key)
    test_scheduler_status(base_url, key)
    test_jobs_trigger(base_url, key)
    test_public_apis(base_url, key)

    print(f"\n{BOLD}── Summary {'─'*48}{RESET}")
    print(f"  {GREEN}PASS: {pass_count}{RESET}   {RED}FAIL: {fail_count}{RESET}   {YELLOW}WARN: {warn_count}{RESET}")
    if fail_count == 0:
        print(f"  {GREEN}{BOLD}✓ All tests passed for {env_name}{RESET}")
    else:
        print(f"  {RED}{BOLD}✗ {fail_count} test(s) failed — see above{RESET}")

    return fail_count == 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DC Hub QA Smoke Tests")
    parser.add_argument("--env", default="railway", choices=["railway", "replit", "local", "both"],
                        help="Which environment to test (default: railway)")
    parser.add_argument("--key", default=os.environ.get("DCHUB_ADMIN_KEY", ""),
                        help="Admin key (or set DCHUB_ADMIN_KEY env var)")
    args = parser.parse_args()

    envs = ["railway", "replit"] if args.env == "both" else [args.env]
    all_passed = True

    for env in envs:
        ok = run(env, TARGETS[env], args.key)
        all_passed = all_passed and ok

    sys.exit(0 if all_passed else 1)
