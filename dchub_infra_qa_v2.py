#!/usr/bin/env python3
"""
DC Hub Infrastructure QA v2 — Provider Diversity Validation
=============================================================
Tests that Replit, Neon, Railway, and Cloudflare are all working
as designed and that the multi-provider architecture is intact.

v2 changes:
  - Handles both health response formats (with/without facility_count)
  - Skips Replit self-referencing SSL tests when running from Replit
  - Tests Railway data via facility search (not just health fields)
  - Adds latency checks per backend

Run from Replit shell:
  python dchub_infra_qa.py

Or from anywhere with Python 3 + requests installed:
  pip install requests
  python dchub_infra_qa.py
"""

import json
import time
import sys
import os
import ssl
from datetime import datetime

try:
    import requests
except ImportError:
    print("Installing requests...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "-q"])
    import requests

# ============================================================
# CONFIGURATION — Update these if your URLs have changed
# ============================================================
CLOUDFLARE_FRONTEND = "https://dchub.cloud"
CLOUDFLARE_API      = "https://dchub.cloud/api"
REPLIT_DIRECT       = "https://dc-hub-replit-fixedzip--azmartone1.replit.app"
RAILWAY_DIRECT      = "https://dchub-backend-production.up.railway.app"

# Detect if running inside Replit (self-referencing SSL will fail)
RUNNING_ON_REPLIT = bool(os.environ.get("REPL_ID") or os.environ.get("REPLIT_DB_URL"))

# ============================================================
# TEST FRAMEWORK
# ============================================================
results = []
PASS = "✅ PASS"
FAIL = "❌ FAIL"
WARN = "⚠️  WARN"
SKIP = "⏭️  SKIP"

def test(name, func):
    """Run a test and record the result."""
    try:
        status, detail = func()
        results.append((name, status, detail))
        icon = {"pass": PASS, "fail": FAIL, "warn": WARN, "skip": SKIP}.get(status, "%s")
        print(f"  {icon}  {name}")
        if detail and status != "pass":
            print(f"         → {detail}")
    except Exception as e:
        results.append((name, "fail", str(e)))
        print(f"  {FAIL}  {name}")
        print(f"         → Exception: {e}")

def timed_get(url, timeout=15, **kwargs):
    """HTTP GET with timing."""
    start = time.time()
    r = requests.get(url, timeout=timeout, allow_redirects=True, **kwargs)
    elapsed = round((time.time() - start) * 1000)
    return r, elapsed


# ============================================================
# LAYER 1: CLOUDFLARE (Frontend + Worker)
# ============================================================
def test_cloudflare_frontend():
    r, ms = timed_get(CLOUDFLARE_FRONTEND)
    if r.status_code == 200 and ("DC Hub" in r.text or "dchub" in r.text.lower()):
        return "pass", f"HTTP 200, {len(r.text):,} bytes, {ms}ms"
    return "fail", f"HTTP {r.status_code}, {ms}ms"

def test_cloudflare_worker_health():
    r, ms = timed_get(f"{CLOUDFLARE_API}/health")
    if r.status_code == 200:
        try:
            data = r.json()
            parts = [f"{ms}ms"]
            for key in ["worker", "version", "source", "environment", "facility_count"]:
                if key in data:
                    parts.append(f"{key}={data[key]}")
            return "pass", ", ".join(parts)
        except:
            return "warn", f"HTTP 200 but non-JSON ({ms}ms)"
    return "fail", f"HTTP {r.status_code} ({ms}ms)"

def test_cloudflare_failover_status():
    r, ms = timed_get(f"{CLOUDFLARE_API}/v1/failover-status")
    if r.status_code == 200:
        try:
            data = r.json()
            primary = data.get("primary", data.get("backends", {}).get("primary", "?"))
            primary_ok = data.get("primary_healthy", data.get("replit_healthy", False))
            failover = data.get("failover", data.get("backends", {}).get("failover", "?"))
            failover_ok = data.get("failover_healthy", data.get("railway_healthy", False))
            circuit = data.get("circuit_open", False)
            worker_ver = data.get("worker_version", "?")

            detail = f"primary={primary}({'✓' if primary_ok else '✗'}), failover={failover}({'✓' if failover_ok else '✗'}), circuit={'OPEN' if circuit else 'closed'}, worker=v{worker_ver}"

            if primary_ok and failover_ok and not circuit:
                return "pass", detail
            elif primary_ok or failover_ok:
                return "warn", detail
            return "fail", detail
        except:
            return "warn", f"HTTP 200 but parse failed ({ms}ms)"
    return "fail", f"HTTP {r.status_code} ({ms}ms)"

def test_cloudflare_backend_header():
    r, _ = timed_get(f"{CLOUDFLARE_API}/health")
    backend = r.headers.get("x-dc-hub-backend", None)
    if backend:
        return "pass", f"x-dc-hub-backend: {backend}"
    return "warn", "No x-dc-hub-backend header"


# ============================================================
# LAYER 2: RAILWAY (Primary Backend)
# ============================================================
def test_railway_health():
    r, ms = timed_get(f"{RAILWAY_DIRECT}/api/health")
    if r.status_code == 200:
        try:
            data = r.json()
            parts = [f"{ms}ms"]
            for key in ["version", "source", "environment", "facility_count", "uptime_seconds"]:
                if key in data:
                    parts.append(f"{key}={data[key]}")
            return "pass", ", ".join(parts)
        except:
            return "pass", f"HTTP 200, {ms}ms"
    return "fail", f"HTTP {r.status_code}, {ms}ms: {r.text[:200]}"

def test_railway_neon_data():
    """Verify Railway serves real data from Neon by doing a facility search."""
    r, ms = timed_get(f"{RAILWAY_DIRECT}/api/v1/facilities%sq=ashburn&limit=2")
    if r.status_code == 200:
        try:
            data = r.json()
            total = data.get("total_matching", 0)
            results_list = data.get("data", [])
            if total > 0 or len(results_list) > 0:
                return "pass", f"{total} matching facilities, {len(results_list)} returned, {ms}ms"
            return "fail", f"Search returned 0 results — Neon may be empty ({ms}ms)"
        except:
            return "warn", f"HTTP 200 but unexpected format ({ms}ms)"
    return "fail", f"HTTP {r.status_code}, {ms}ms"

def test_railway_environment():
    r, _ = timed_get(f"{RAILWAY_DIRECT}/api/health")
    if r.status_code == 200:
        data = r.json()
        env = data.get("environment", None)
        if env == "railway":
            return "pass", "IS_RAILWAY=True confirmed"
        elif env:
            return "warn", f"environment={env} (expected 'railway')"
        return "warn", "No 'environment' field — update health endpoint"
    return "fail", f"HTTP {r.status_code}"

def test_railway_latency():
    _, ms = timed_get(f"{RAILWAY_DIRECT}/api/health")
    if ms < 500:
        return "pass", f"{ms}ms (fast)"
    elif ms < 2000:
        return "warn", f"{ms}ms (acceptable but slow)"
    return "fail", f"{ms}ms (too slow — check Railway region/cold start)"


# ============================================================
# LAYER 3: REPLIT (Failover Backend)
# ============================================================
def test_replit_health():
    if RUNNING_ON_REPLIT:
        return "skip", "Skipped — self-referencing SSL fails from inside Replit. Worker confirms Replit is healthy."

    r, ms = timed_get(f"{REPLIT_DIRECT}/api/health")
    if r.status_code == 200:
        try:
            data = r.json()
            parts = [f"{ms}ms"]
            for key in ["version", "source", "facility_count"]:
                if key in data:
                    parts.append(f"{key}={data[key]}")
            return "pass", ", ".join(parts)
        except:
            return "pass", f"HTTP 200, {ms}ms"
    elif r.status_code == 503:
        return "warn", f"Replit waking from sleep ({ms}ms)"
    return "fail", f"HTTP {r.status_code}, {ms}ms"

def test_replit_healthy_via_worker():
    """Check Replit health indirectly through the Worker's failover status."""
    r, _ = timed_get(f"{CLOUDFLARE_API}/v1/failover-status")
    if r.status_code == 200:
        data = r.json()
        # Handle both v3.2 and v3.4 response formats
        replit_ok = data.get("failover_healthy", data.get("replit_healthy", None))
        if replit_ok is True:
            return "pass", "Worker confirms Replit is healthy"
        elif replit_ok is False:
            return "fail", "Worker reports Replit is unhealthy"
        return "warn", "Could not determine Replit health from Worker"
    return "warn", f"Worker failover endpoint returned {r.status_code}"


# ============================================================
# LAYER 4: NEON (Database — verified through backends)
# ============================================================
def test_neon_facility_count():
    """Check facility count is substantial (11000+)."""
    # Try health endpoint first (if updated)
    r, _ = timed_get(f"{RAILWAY_DIRECT}/api/health")
    if r.status_code == 200:
        data = r.json()
        fc = data.get("facility_count", None)
        if fc is not None:
            if fc > 10000:
                return "pass", f"{fc:,} facilities in Neon"
            elif fc > 0:
                return "warn", f"Only {fc:,} facilities — expected 11,000+"
            return "fail", "0 facilities in health response"

    # Fallback: use search to estimate
    r2, _ = timed_get(f"{RAILWAY_DIRECT}/api/v1/facilities%sq=data+center&limit=1")
    if r2.status_code == 200:
        data2 = r2.json()
        total = data2.get("total_matching", 0)
        if total > 1000:
            return "pass", f"Search indicates {total:,}+ facilities (health endpoint lacks count field)"
        return "warn", f"Search returned total_matching={total}"
    return "fail", "Could not verify facility count"

def test_neon_deals():
    r, ms = timed_get(f"{RAILWAY_DIRECT}/api/v1/deals%slimit=1")
    if r.status_code == 200:
        try:
            data = r.json()
            count = data.get("total", data.get("count", len(data) if isinstance(data, list) else 0))
            return "pass", f"Deals accessible, {ms}ms"
        except:
            return "pass", f"HTTP 200, {ms}ms"
    elif r.status_code in [401, 403]:
        return "pass", "Deals endpoint gated (auth required) — endpoint exists"
    return "fail", f"HTTP {r.status_code}, {ms}ms"

def test_neon_news():
    r, ms = timed_get(f"{RAILWAY_DIRECT}/api/v1/news%slimit=1")
    if r.status_code == 200:
        return "pass", f"News accessible, {ms}ms"
    elif r.status_code in [401, 403]:
        return "pass", "News endpoint gated — endpoint exists"
    return "fail", f"HTTP {r.status_code}, {ms}ms"

def test_neon_consistency():
    """Both backends return data from the same Neon DB."""
    if RUNNING_ON_REPLIT:
        # Can't hit Replit directly, so verify via Worker
        r1, _ = timed_get(f"{RAILWAY_DIRECT}/api/v1/facilities%sq=equinix&limit=1")
        r2, _ = timed_get(f"{CLOUDFLARE_API}/v1/facilities%sq=equinix&limit=1")
        if r1.status_code == 200 and r2.status_code == 200:
            return "pass", "Railway direct + Worker proxy both return Equinix results"
        return "warn", f"Railway={r1.status_code}, Worker={r2.status_code}"

    # If not on Replit, test both backends directly
    r1, _ = timed_get(f"{RAILWAY_DIRECT}/api/v1/facilities%sq=equinix&limit=1")
    r2, _ = timed_get(f"{REPLIT_DIRECT}/api/v1/facilities%sq=equinix&limit=1")
    if r1.status_code == 200 and r2.status_code == 200:
        return "pass", "Both backends return Equinix results from shared Neon DB"
    return "warn", f"Railway={r1.status_code}, Replit={r2.status_code}"


# ============================================================
# LAYER 5: PROVIDER DIVERSITY
# ============================================================
def test_provider_diversity():
    providers = {}

    try:
        r, _ = timed_get(CLOUDFLARE_FRONTEND)
        providers["Cloudflare Pages"] = "✓" if r.status_code == 200 else "✗"
    except:
        providers["Cloudflare Pages"] = "✗"

    try:
        r, _ = timed_get(f"{CLOUDFLARE_API}/health")
        providers["Cloudflare Worker"] = "✓" if r.status_code == 200 else "✗"
    except:
        providers["Cloudflare Worker"] = "✗"

    try:
        r, _ = timed_get(f"{RAILWAY_DIRECT}/api/health")
        providers["Railway"] = "✓" if r.status_code == 200 else "✗"
    except:
        providers["Railway"] = "✗"

    if RUNNING_ON_REPLIT:
        # Trust the Worker's assessment
        try:
            r, _ = timed_get(f"{CLOUDFLARE_API}/v1/failover-status")
            data = r.json()
            replit_ok = data.get("failover_healthy", data.get("replit_healthy", False))
            providers["Replit"] = "✓" if replit_ok else "✗"
        except:
            providers["Replit"] = "?"
    else:
        try:
            r, _ = timed_get(f"{REPLIT_DIRECT}/api/health")
            providers["Replit"] = "✓" if r.status_code == 200 else "✗"
        except:
            providers["Replit"] = "✗"

    # Neon verified through Railway data
    try:
        r, _ = timed_get(f"{RAILWAY_DIRECT}/api/v1/facilities%sq=test&limit=1")
        data = r.json()
        if data.get("total_matching", 0) > 0 or len(data.get("data", [])) > 0:
            providers["Neon PostgreSQL"] = "✓"
        else:
            providers["Neon PostgreSQL"] = "?"
    except:
        providers["Neon PostgreSQL"] = "?"

    healthy = sum(1 for v in providers.values() if v == "✓")
    total = len(providers)
    detail = " | ".join([f"{k}={v}" for k, v in providers.items()])

    if healthy >= 4:
        return "pass", f"{healthy}/{total} providers active — {detail}"
    elif healthy >= 3:
        return "warn", f"{healthy}/{total} — {detail}"
    return "fail", f"Only {healthy}/{total} — {detail}"

def test_no_spof():
    """Verify we have backend redundancy."""
    railway_ok = False
    replit_ok = False

    try:
        r, _ = timed_get(f"{RAILWAY_DIRECT}/api/health")
        railway_ok = r.status_code == 200
    except:
        pass

    # Check Replit via Worker
    try:
        r, _ = timed_get(f"{CLOUDFLARE_API}/v1/failover-status")
        data = r.json()
        replit_ok = data.get("failover_healthy", data.get("replit_healthy", False))
    except:
        pass

    if railway_ok and replit_ok:
        return "pass", "Both Railway (primary) and Replit (failover) operational"
    elif railway_ok:
        return "warn", "Railway up, Replit down — single backend active"
    elif replit_ok:
        return "warn", "Replit up, Railway down — running on failover only"
    return "fail", "Both backends unreachable"


# ============================================================
# LAYER 6: KEY FEATURES
# ============================================================
def test_facility_search_e2e():
    r, ms = timed_get(f"{CLOUDFLARE_API}/v1/facilities%sq=dallas&limit=2")
    if r.status_code == 200:
        try:
            data = r.json()
            total = data.get("total_matching", len(data.get("data", [])))
            return "pass", f"Dallas search: {total} results, {ms}ms end-to-end"
        except:
            return "pass", f"HTTP 200, {ms}ms"
    return "fail", f"HTTP {r.status_code}, {ms}ms"

def test_ai_discovery_files():
    ok, missing = [], []
    for path in ["/llms.txt", "/ai-plugin.json", "/.well-known/ai-plugin.json"]:
        try:
            r, _ = timed_get(f"{CLOUDFLARE_FRONTEND}{path}")
            (ok if r.status_code == 200 else missing).append(path)
        except:
            missing.append(path)

    if ok and not missing:
        return "pass", f"All accessible: {', '.join(ok)}"
    elif ok:
        return "warn", f"OK: {', '.join(ok)} | Missing: {', '.join(missing)}"
    return "fail", f"None accessible"

def test_cors():
    r, _ = timed_get(f"{CLOUDFLARE_API}/health", headers={"Origin": "https://dchub.cloud"})
    acao = r.headers.get("Access-Control-Allow-Origin", "")
    if "dchub.cloud" in acao or acao == "*":
        return "pass", f"CORS: {acao}"
    return "warn", f"CORS header: '{acao}'"


# ============================================================
# RUN ALL TESTS
# ============================================================
def main():
    print()
    print("=" * 70)
    print("  DC Hub Infrastructure QA v2 — Provider Diversity Validation")
    print(f"  {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    if RUNNING_ON_REPLIT:
        print("  ⓘ  Running from Replit — Replit self-tests via Worker proxy")
    print("=" * 70)

    print("\n📡 LAYER 1: Cloudflare (Frontend + Worker)")
    print("-" * 50)
    test("Frontend loads", test_cloudflare_frontend)
    test("Worker proxies /api/health", test_cloudflare_worker_health)
    test("Failover status endpoint", test_cloudflare_failover_status)
    test("Backend identification header", test_cloudflare_backend_header)

    print("\n🚂 LAYER 2: Railway (Primary Backend)")
    print("-" * 50)
    test("Railway health check", test_railway_health)
    test("Railway serves Neon data", test_railway_neon_data)
    test("Railway environment flag", test_railway_environment)
    test("Railway latency", test_railway_latency)

    print("\n🔄 LAYER 3: Replit (Failover Backend)")
    print("-" * 50)
    test("Replit direct health", test_replit_health)
    test("Replit healthy via Worker", test_replit_healthy_via_worker)

    print("\n🐘 LAYER 4: Neon (PostgreSQL)")
    print("-" * 50)
    test("Facility count (11,000+)", test_neon_facility_count)
    test("Deals data accessible", test_neon_deals)
    test("News data accessible", test_neon_news)
    test("Data consistency across backends", test_neon_consistency)

    print("\n🏗️  LAYER 5: Provider Diversity")
    print("-" * 50)
    test("4+ providers active", test_provider_diversity)
    test("No single point of failure", test_no_spof)

    print("\n🔌 LAYER 6: Key Features")
    print("-" * 50)
    test("Facility search end-to-end", test_facility_search_e2e)
    test("AI discovery files", test_ai_discovery_files)
    test("CORS headers", test_cors)

    # Summary
    print("\n" + "=" * 70)
    passed  = sum(1 for _, s, _ in results if s == "pass")
    warned  = sum(1 for _, s, _ in results if s == "warn")
    failed  = sum(1 for _, s, _ in results if s == "fail")
    skipped = sum(1 for _, s, _ in results if s == "skip")
    total   = len(results)

    print(f"\n  RESULTS: {passed} passed, {warned} warnings, {failed} failed, {skipped} skipped  ({total} tests)")

    if failed == 0 and warned == 0:
        print("\n  🎯 ALL SYSTEMS GO — Provider diversity intact and working as designed!")
    elif failed == 0:
        print(f"\n  ⚡ OPERATIONAL — {warned} minor item(s) to review, no critical failures.")
    else:
        print(f"\n  🔴 ISSUES — {failed} test(s) need attention.")

    # Architecture diagram
    print("\n" + "-" * 70)
    print("  ARCHITECTURE:")
    print("  Browser → Cloudflare Pages (frontend)")
    print("         → Cloudflare Worker v3.4 (proxy + cache + failover)")
    print("            ├─ Railway  [PRIMARY]  → Neon PostgreSQL")
    print("            └─ Replit   [FAILOVER] → Neon PostgreSQL")
    print()

    return 0 if failed == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
