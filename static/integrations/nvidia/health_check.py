#!/usr/bin/env python3
"""
DC Hub × NVIDIA — 8-Endpoint Health Check
Runs the verification suite and reports pass/fail.
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

API_KEY = os.environ.get("DC_HUB_API_KEY", "dchub_nvidia_2026_verify")
BASE_URL = os.environ.get("DC_HUB_BASE_URL", "https://dchub.cloud/api")

HEADERS = {
    "X-API-Key": API_KEY,
    "Accept": "application/json",
    "User-Agent": "NVIDIA-DC-Hub-HealthCheck/1.0"
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
        except Exception as e:
            results.append({
                "endpoint": path,
                "status": "ERROR",
                "error": str(e)
            })

    passed = sum(1 for r in results if r["status"] == "PASS")
    report = {
        "platform": "NVIDIA",
        "key": API_KEY[:20] + "...",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "passed": f"{passed}/8",
        "results": results
    }

    print(json.dumps(report, indent=2))
    return passed == 8

if __name__ == "__main__":
    success = run()
    sys.exit(0 if success else 1)
