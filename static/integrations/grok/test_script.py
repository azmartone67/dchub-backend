#!/usr/bin/env python3
"""DC Hub API Verification Script — Grok (xAI)
Run this to verify all 8 core endpoints."""

import requests
import json
import sys

BASE = "https://dc-hub-replit-fixedzip--azmartone1.replit.app"
KEY = "dchub_grok_2026_verify"
HEADERS = {"X-API-Key": KEY}

ENDPOINTS = [
    ("/api/agent/facilities?q=Equinix&country=US&limit=5", "Facility Search"),
    ("/api/agent/stats", "Platform Stats"),
    ("/api/transactions?limit=10", "Transactions"),
    ("/api/news?limit=5", "News Feed"),
    ("/api/stats", "Summary Stats"),
    ("/api/v1/markets/list", "Markets List"),
    ("/api/v1/lmp/prices", "LMP Prices"),
    ("/api/v1/pipeline", "Pipeline Data"),
]

def run_tests():
    passed = 0
    failed = 0
    results = []

    for path, name in ENDPOINTS:
        url = f"{BASE}{path}"
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            status = "PASS" if r.status_code == 200 else f"FAIL ({r.status_code})"
            if r.status_code == 200:
                passed += 1
                data = r.json()
                preview = json.dumps(data, indent=2)[:200]
            else:
                failed += 1
                preview = r.text[:200]
            results.append((name, status, preview))
            print(f"  {status} | {name} | {url}")
        except Exception as e:
            failed += 1
            results.append((name, f"ERROR: {e}", ""))
            print(f"  ERROR | {name} | {e}")

    print(f"\n=== RESULTS: {passed}/{len(ENDPOINTS)} passed, {failed} failed ===")
    return passed, failed, results

if __name__ == "__main__":
    print(f"DC Hub API Verification — Grok (xAI)")
    print(f"Base URL: {BASE}")
    print(f"API Key: {KEY[:20]}...")
    print(f"Testing {len(ENDPOINTS)} endpoints...\n")
    passed, failed, results = run_tests()
    sys.exit(0 if failed == 0 else 1)
