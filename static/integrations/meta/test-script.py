#!/usr/bin/env python3
"""
DC Hub x Meta AI — 8-Endpoint Verification Test
Run locally: python test-script.py
"""
import os
import sys
import json
import time

try:
    import requests
except ImportError:
    os.system("pip install requests")
    import requests

API_KEY = os.environ.get("DC_HUB_API_KEY", "dchub_meta_2026_verify")
BASE_URL = "https://dchub.cloud/api"

HEADERS = {
    "X-API-Key": API_KEY,
    "Accept": "application/json",
    "User-Agent": "Meta-AI-DC-Hub-Integration/1.0"
}

ENDPOINTS = [
    ("/agent/facilities", {"q": "Equinix", "country": "US"}),
    ("/agent/stats", {}),
    ("/transactions", {"limit": 10}),
    ("/news", {"limit": 5}),
    ("/stats", {}),
    ("/v1/markets/list", {}),
    ("/v1/lmp/prices", {}),
    ("/v1/pipeline", {}),
]

def run():
    print(f"DC Hub x Meta AI Verification")
    print(f"Key: {API_KEY[:20]}...")
    print(f"Time: {time.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    print("-" * 50)

    results = []
    for path, params in ENDPOINTS:
        url = f"{BASE_URL}{path}"
        start = time.time()
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=15)
            elapsed = round((time.time() - start) * 1000, 1)
            passed = r.status_code == 200
            results.append({
                "endpoint": path,
                "status": "PASS" if passed else "FAIL",
                "code": r.status_code,
                "latency_ms": elapsed
            })
            icon = "\u2705" if passed else "\u274c"
            print(f"  {icon} {path} -- {r.status_code} ({elapsed}ms)")
        except Exception as e:
            results.append({
                "endpoint": path,
                "status": "ERROR",
                "error": str(e)
            })
            print(f"  \u274c {path} -- ERROR: {e}")

    passed = sum(1 for r in results if r["status"] == "PASS")
    print("-" * 50)
    print(f"Result: {passed}/8 passed")

    report = {
        "platform": "meta",
        "key": API_KEY[:20] + "...",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "passed": f"{passed}/8",
        "results": results
    }

    with open("meta-verification-report.json", "w") as f:
        json.dump(report, f, indent=2)
    print(f"Report saved: meta-verification-report.json")

    return passed == 8

if __name__ == "__main__":
    success = run()
    sys.exit(0 if success else 1)
