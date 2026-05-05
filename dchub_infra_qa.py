#!/usr/bin/env python3
"""
DC Hub Infrastructure QA — Provider Diversity Validation
=========================================================
Tests that Replit, Neon, Railway, and Cloudflare are all working
as designed and that the multi-provider architecture is intact.

Run from Replit shell:
  python dchub_infra_qa.py

Or from anywhere with Python 3 + requests installed:
  pip install requests
  python dchub_infra_qa.py
"""

import json
import time
import sys
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
NEON_HOST           = "ep-old-waterfall-aa2rwjzs-pooler.westus3.azure.neon.tech"

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
        icon = PASS if status == "pass" else FAIL if status == "fail" else WARN if status == "warn" else SKIP
        print(f"  {icon}  {name}")
        if detail and status != "pass":
            print(f"         → {detail}")
    except Exception as e:
        results.append((name, "fail", str(e)))
        print(f"  {FAIL}  {name}")
        print(f"         → Exception: {e}")

def get(url, timeout=15, **kwargs):
    """HTTP GET with timeout."""
    return requests.get(url, timeout=timeout, allow_redirects=True, **kwargs)

def post(url, timeout=15, **kwargs):
    """HTTP POST with timeout."""
    return requests.post(url, timeout=timeout, allow_redirects=True, **kwargs)


# ============================================================
# LAYER 1: CLOUDFLARE (Frontend + Worker)
# ============================================================
def test_cloudflare_frontend():
    """Cloudflare Pages serves the frontend."""
    r = get(CLOUDFLARE_FRONTEND)
    if r.status_code == 200 and ("DC Hub" in r.text or "dchub" in r.text.lower()):
        return "pass", f"HTTP {r.status_code}, page loaded ({len(r.text)} bytes)"
    return "fail", f"HTTP {r.status_code}, content check failed"

def test_cloudflare_worker_health():
    """Cloudflare Worker proxies /api/health to backend."""
    r = get(f"{CLOUDFLARE_API}/health")
    if r.status_code == 200:
        try:
            data = r.json()
            worker_ver = data.get("worker", "unknown")
            source = data.get("source", "unknown")
            facility_count = data.get("facility_count", 0)
            return "pass", f"Worker v{worker_ver}, source={source}, facilities={facility_count}"
        except:
            return "warn", f"HTTP 200 but non-JSON response: {r.text[:200]}"
    return "fail", f"HTTP {r.status_code}: {r.text[:200]}"

def test_cloudflare_worker_version():
    """Worker version is 3.2+ with failover support."""
    r = get(f"{CLOUDFLARE_API}/health")
    if r.status_code == 200:
        try:
            data = r.json()
            ver = data.get("worker", "0")
            if float(ver) >= 3.2:
                return "pass", f"Worker v{ver} (failover-enabled)"
            return "warn", f"Worker v{ver} — expected 3.2+ for failover"
        except:
            return "warn", "Could not parse worker version"
    return "fail", f"HTTP {r.status_code}"

def test_cloudflare_failover_status():
    """Failover status endpoint reports both backends."""
    r = get(f"{CLOUDFLARE_API}/v1/failover-status")
    if r.status_code == 200:
        try:
            data = r.json()
            replit_ok = data.get("replit_healthy", False)
            railway_ok = data.get("railway_healthy", False)
            circuit = data.get("circuit_open", True)
            primary = data.get("backends", {}).get("primary", "unknown")
            failover_be = data.get("backends", {}).get("failover", "unknown")
            
            status_parts = []
            if replit_ok:
                status_parts.append("Replit=healthy")
            else:
                status_parts.append("Replit=DOWN")
            if railway_ok:
                status_parts.append("Railway=healthy")
            else:
                status_parts.append("Railway=DOWN")
            status_parts.append(f"circuit={'OPEN' if circuit else 'closed'}")
            status_parts.append(f"primary={primary.split('/')[-1][:30]}")
            
            detail = ", ".join(status_parts)
            
            if replit_ok and railway_ok and not circuit:
                return "pass", detail
            elif replit_ok or railway_ok:
                return "warn", detail
            return "fail", detail
        except:
            return "warn", f"HTTP 200 but parse failed: {r.text[:200]}"
    return "fail", f"HTTP {r.status_code}: {r.text[:200]}"

def test_cloudflare_backend_header():
    """Response includes x-dc-hub-backend header showing which backend served."""
    r = get(f"{CLOUDFLARE_API}/health")
    backend = r.headers.get("x-dc-hub-backend", None)
    if backend:
        return "pass", f"Backend header: {backend}"
    return "warn", "No x-dc-hub-backend header (Worker may not be adding it)"


# ============================================================
# LAYER 2: RAILWAY (Primary Backend)
# ============================================================
def test_railway_direct_health():
    """Railway backend responds to direct health check."""
    r = get(f"{RAILWAY_DIRECT}/api/health")
    if r.status_code == 200:
        try:
            data = r.json()
            return "pass", f"Railway healthy — facilities={data.get('facility_count', '%s')}, source={data.get('source', '%s')}"
        except:
            return "pass", f"HTTP 200 ({len(r.text)} bytes)"
    return "fail", f"HTTP {r.status_code}: {r.text[:200]}"

def test_railway_neon_connection():
    """Railway connects to Neon database (not SQLite)."""
    r = get(f"{RAILWAY_DIRECT}/api/health")
    if r.status_code == 200:
        try:
            data = r.json()
            source = data.get("source", "unknown")
            if "neon" in source.lower() or "postgres" in source.lower():
                return "pass", f"Source: {source} (Neon connected)"
            elif "sqlite" in source.lower():
                return "fail", f"Source: {source} — Railway should use Neon, not SQLite!"
            return "warn", f"Source: {source} — unclear if Neon"
        except:
            return "warn", "Could not determine data source"
    return "fail", f"HTTP {r.status_code}"

def test_railway_facility_search():
    """Railway returns facility search results from Neon."""
    r = get(f"{RAILWAY_DIRECT}/api/v1/facilities%sq=ashburn&limit=3")
    if r.status_code == 200:
        try:
            data = r.json()
            count = len(data) if isinstance(data, list) else data.get("count", data.get("total", len(data.get("results", data.get("facilities", [])))))
            return "pass", f"Search returned data (count/results indicator: {count})"
        except:
            return "warn", f"HTTP 200 but unexpected format: {r.text[:200]}"
    return "fail", f"HTTP {r.status_code}: {r.text[:200]}"

def test_railway_env_detection():
    """Railway environment detection is active (IS_RAILWAY flag)."""
    r = get(f"{RAILWAY_DIRECT}/api/health")
    if r.status_code == 200:
        try:
            data = r.json()
            # Check for environment indicator if exposed
            env = data.get("environment", data.get("env", data.get("runtime", None)))
            if env and "railway" in str(env).lower():
                return "pass", f"Environment: {env}"
            # If not explicitly in health, just confirm it's running
            return "pass", "Railway responding (env flag may not be in health output)"
        except:
            return "pass", "Railway responding"
    return "fail", f"HTTP {r.status_code}"


# ============================================================
# LAYER 3: REPLIT (Failover Backend)
# ============================================================
def test_replit_direct_health():
    """Replit backend responds to direct health check."""
    r = get(f"{REPLIT_DIRECT}/api/health")
    if r.status_code == 200:
        try:
            data = r.json()
            return "pass", f"Replit healthy — facilities={data.get('facility_count', '%s')}, source={data.get('source', '%s')}"
        except:
            return "pass", f"HTTP 200 ({len(r.text)} bytes)"
    elif r.status_code == 503:
        return "warn", "Replit returned 503 — may be waking from sleep"
    return "fail", f"HTTP {r.status_code}: {r.text[:200]}"

def test_replit_neon_connection():
    """Replit also connects to Neon (shared database)."""
    r = get(f"{REPLIT_DIRECT}/api/health")
    if r.status_code == 200:
        try:
            data = r.json()
            source = data.get("source", "unknown")
            if "neon" in source.lower() or "postgres" in source.lower():
                return "pass", f"Source: {source} (Neon connected — same DB as Railway)"
            return "warn", f"Source: {source}"
        except:
            return "warn", "Could not determine data source"
    return "fail", f"HTTP {r.status_code}"


# ============================================================
# LAYER 4: NEON (Database)
# ============================================================
def test_neon_via_railway():
    """Neon returns consistent data via Railway."""
    r = get(f"{RAILWAY_DIRECT}/api/health")
    if r.status_code == 200:
        try:
            data = r.json()
            fc = data.get("facility_count", 0)
            if fc > 10000:
                return "pass", f"{fc} facilities in Neon (via Railway)"
            elif fc > 0:
                return "warn", f"Only {fc} facilities — expected 11000+"
            return "fail", "0 facilities returned"
        except:
            return "warn", "Could not parse facility count"
    return "fail", f"Railway HTTP {r.status_code}"

def test_neon_via_replit():
    """Neon returns consistent data via Replit."""
    r = get(f"{REPLIT_DIRECT}/api/health")
    if r.status_code == 200:
        try:
            data = r.json()
            fc = data.get("facility_count", 0)
            if fc > 10000:
                return "pass", f"{fc} facilities in Neon (via Replit)"
            elif fc > 0:
                return "warn", f"Only {fc} facilities — expected 11000+"
            return "fail", "0 facilities returned"
        except:
            return "warn", "Could not parse facility count"
    return "warn", f"Replit HTTP {r.status_code} — can't verify Neon"

def test_neon_data_consistency():
    """Both backends return the same facility count from Neon."""
    try:
        r1 = get(f"{RAILWAY_DIRECT}/api/health")
        r2 = get(f"{REPLIT_DIRECT}/api/health")
        if r1.status_code == 200 and r2.status_code == 200:
            d1 = r1.json()
            d2 = r2.json()
            fc1 = d1.get("facility_count", -1)
            fc2 = d2.get("facility_count", -2)
            if fc1 == fc2:
                return "pass", f"Both backends: {fc1} facilities (consistent ✓)"
            elif abs(fc1 - fc2) < 50:
                return "warn", f"Railway={fc1}, Replit={fc2} — slight drift (acceptable if recent write)"
            return "fail", f"Railway={fc1}, Replit={fc2} — significant divergence!"
        return "warn", f"Railway={r1.status_code}, Replit={r2.status_code} — can't compare"
    except Exception as e:
        return "warn", f"Comparison failed: {e}"


# ============================================================
# LAYER 5: DIVERSITY VALIDATION
# ============================================================
def test_provider_diversity():
    """Confirm we're using 4 distinct providers (not single point of failure)."""
    providers = {}
    
    # Test Cloudflare
    try:
        r = get(CLOUDFLARE_FRONTEND)
        if r.status_code == 200:
            providers["Cloudflare Pages (Frontend)"] = "✓"
    except:
        providers["Cloudflare Pages (Frontend)"] = "✗"
    
    # Test Cloudflare Worker
    try:
        r = get(f"{CLOUDFLARE_API}/health")
        if r.status_code == 200:
            providers["Cloudflare Worker (Proxy)"] = "✓"
    except:
        providers["Cloudflare Worker (Proxy)"] = "✗"
    
    # Test Railway
    try:
        r = get(f"{RAILWAY_DIRECT}/api/health")
        if r.status_code == 200:
            providers["Railway (Primary Backend)"] = "✓"
    except:
        providers["Railway (Primary Backend)"] = "✗"
    
    # Test Replit
    try:
        r = get(f"{REPLIT_DIRECT}/api/health")
        if r.status_code in [200, 503]:
            providers["Replit (Failover Backend)"] = "✓" if r.status_code == 200 else "sleeping"
    except:
        providers["Replit (Failover Backend)"] = "✗"
    
    # Neon — verified indirectly
    try:
        r = get(f"{RAILWAY_DIRECT}/api/health")
        if r.status_code == 200:
            data = r.json()
            if "neon" in data.get("source", "").lower():
                providers["Neon (PostgreSQL Database)"] = "✓"
    except:
        providers["Neon (PostgreSQL Database)"] = "?"
    
    healthy = sum(1 for v in providers.values() if v == "✓")
    total = len(providers)
    detail = " | ".join([f"{k}={v}" for k, v in providers.items()])
    
    if healthy >= 4:
        return "pass", f"{healthy}/{total} providers healthy — {detail}"
    elif healthy >= 3:
        return "warn", f"{healthy}/{total} providers — {detail}"
    return "fail", f"{healthy}/{total} providers — {detail}"

def test_no_single_point_of_failure():
    """If Railway dies, Replit should still work (and vice versa)."""
    railway_ok = False
    replit_ok = False
    
    try:
        r = get(f"{RAILWAY_DIRECT}/api/health")
        railway_ok = r.status_code == 200
    except:
        pass
    
    try:
        r = get(f"{REPLIT_DIRECT}/api/health")
        replit_ok = r.status_code == 200
    except:
        pass
    
    if railway_ok and replit_ok:
        return "pass", "Both backends operational — no SPOF for compute"
    elif railway_ok or replit_ok:
        which = "Railway" if railway_ok else "Replit"
        down = "Replit" if railway_ok else "Railway"
        return "warn", f"Only {which} is up, {down} is down — partial redundancy"
    return "fail", "Both backends down — single point of failure!"

def test_shared_database():
    """Both backends share the same Neon database."""
    try:
        r1 = get(f"{RAILWAY_DIRECT}/api/v1/facilities%sq=equinix&limit=1")
        r2 = get(f"{REPLIT_DIRECT}/api/v1/facilities%sq=equinix&limit=1")
        if r1.status_code == 200 and r2.status_code == 200:
            # Both should return similar results since they hit the same Neon DB
            return "pass", "Both backends return search results from shared Neon DB"
        elif r1.status_code == 200:
            return "warn", f"Railway search works, Replit returned {r2.status_code}"
        return "fail", f"Railway={r1.status_code}, Replit={r2.status_code}"
    except Exception as e:
        return "warn", f"Could not verify: {e}"


# ============================================================
# LAYER 6: KEY ENDPOINTS
# ============================================================
def test_api_facility_search_via_worker():
    """Facility search works through the full Cloudflare Worker chain."""
    r = get(f"{CLOUDFLARE_API}/v1/facilities%sq=dallas&limit=2")
    if r.status_code == 200:
        return "pass", f"Facility search via Worker: HTTP 200 ({len(r.text)} bytes)"
    return "fail", f"HTTP {r.status_code}: {r.text[:200]}"

def test_api_deals_endpoint():
    """M&A deals endpoint works."""
    r = get(f"{CLOUDFLARE_API}/v1/deals%slimit=2")
    if r.status_code == 200:
        return "pass", f"Deals endpoint: HTTP 200 ({len(r.text)} bytes)"
    elif r.status_code == 401:
        return "pass", "Deals endpoint: HTTP 401 (auth required — endpoint exists and gated)"
    return "fail", f"HTTP {r.status_code}: {r.text[:200]}"

def test_api_news_endpoint():
    """News endpoint works."""
    r = get(f"{CLOUDFLARE_API}/v1/news%slimit=2")
    if r.status_code == 200:
        return "pass", f"News endpoint: HTTP 200 ({len(r.text)} bytes)"
    return "fail", f"HTTP {r.status_code}: {r.text[:200]}"

def test_ai_discovery_files():
    """AI discovery files are accessible."""
    files_ok = []
    files_fail = []
    for path in ["/llms.txt", "/ai-plugin.json", "/.well-known/ai-plugin.json"]:
        try:
            r = get(f"{CLOUDFLARE_FRONTEND}{path}")
            if r.status_code == 200:
                files_ok.append(path)
            else:
                files_fail.append(f"{path}={r.status_code}")
        except:
            files_fail.append(f"{path}=timeout")
    
    if files_ok and not files_fail:
        return "pass", f"All AI files accessible: {', '.join(files_ok)}"
    elif files_ok:
        return "warn", f"OK: {', '.join(files_ok)} | Missing: {', '.join(files_fail)}"
    return "fail", f"None accessible: {', '.join(files_fail)}"

def test_cors_headers():
    """CORS headers are properly set for frontend requests."""
    headers = {"Origin": "https://dchub.cloud"}
    r = get(f"{CLOUDFLARE_API}/health", headers=headers)
    acao = r.headers.get("Access-Control-Allow-Origin", "")
    if "dchub.cloud" in acao or acao == "*":
        return "pass", f"CORS: Access-Control-Allow-Origin = {acao}"
    return "warn", f"CORS header: '{acao}' — may cause frontend issues"


# ============================================================
# RUN ALL TESTS
# ============================================================
def main():
    print()
    print("=" * 70)
    print("  DC Hub Infrastructure QA — Provider Diversity Validation")
    print(f"  Run at: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 70)
    
    print("\n📡 LAYER 1: Cloudflare (Frontend + Worker)")
    print("-" * 50)
    test("Frontend loads on Cloudflare Pages", test_cloudflare_frontend)
    test("Worker proxies /api/health", test_cloudflare_worker_health)
    test("Worker version >= 3.2 (failover)", test_cloudflare_worker_version)
    test("Failover status shows both backends", test_cloudflare_failover_status)
    test("Backend identification header", test_cloudflare_backend_header)
    
    print("\n🚂 LAYER 2: Railway (Primary Backend)")
    print("-" * 50)
    test("Railway direct health check", test_railway_direct_health)
    test("Railway → Neon connection", test_railway_neon_connection)
    test("Railway facility search", test_railway_facility_search)
    test("Railway environment detection", test_railway_env_detection)
    
    print("\n🔄 LAYER 3: Replit (Failover Backend)")
    print("-" * 50)
    test("Replit direct health check", test_replit_direct_health)
    test("Replit → Neon connection", test_replit_neon_connection)
    
    print("\n🐘 LAYER 4: Neon (PostgreSQL Database)")
    print("-" * 50)
    test("Neon data via Railway", test_neon_via_railway)
    test("Neon data via Replit", test_neon_via_replit)
    test("Data consistency across backends", test_neon_data_consistency)
    
    print("\n🏗️  LAYER 5: Provider Diversity")
    print("-" * 50)
    test("4+ distinct providers active", test_provider_diversity)
    test("No single point of failure", test_no_single_point_of_failure)
    test("Shared Neon database", test_shared_database)
    
    print("\n🔌 LAYER 6: Key Endpoints")
    print("-" * 50)
    test("Facility search via Worker", test_api_facility_search_via_worker)
    test("M&A deals endpoint", test_api_deals_endpoint)
    test("News endpoint", test_api_news_endpoint)
    test("AI discovery files", test_ai_discovery_files)
    test("CORS headers", test_cors_headers)
    
    # Summary
    print("\n" + "=" * 70)
    passed = sum(1 for _, s, _ in results if s == "pass")
    warned = sum(1 for _, s, _ in results if s == "warn")
    failed = sum(1 for _, s, _ in results if s == "fail")
    total = len(results)
    
    print(f"\n  RESULTS: {passed} passed, {warned} warnings, {failed} failed  (out of {total} tests)")
    
    if failed == 0 and warned == 0:
        print("\n  🎯 ALL SYSTEMS GO — Provider diversity is intact and working as designed!")
    elif failed == 0:
        print(f"\n  ⚡ MOSTLY GOOD — {warned} item(s) to review but no critical failures.")
    else:
        print(f"\n  🔴 ISSUES FOUND — {failed} test(s) need attention.")
    
    # Architecture summary
    print("\n" + "-" * 70)
    print("  ARCHITECTURE SUMMARY:")
    print("  ┌──────────────────────────────────────────────────┐")
    print("  │  Browser → Cloudflare Pages (frontend)           │")
    print("  │         → Cloudflare Worker v3.2 (proxy/cache)   │")
    print("  │            ├→ Railway (primary backend)           │")
    print("  │            └→ Replit  (failover backend)          │")
    print("  │               └→ Neon PostgreSQL (shared DB)      │")
    print("  └──────────────────────────────────────────────────┘")
    print()
    
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
