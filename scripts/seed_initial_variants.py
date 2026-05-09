#!/usr/bin/env python3
"""Seed initial A/B variants for /dcpi — run via Railway shell or as a
one-off after deploy. Idempotent."""
import os, json, urllib.request

API = os.environ.get("DCHUB_API_BASE", "https://dchub.cloud")

VARIANTS = [
    # Hero copy A/B
    {"surface": "/dcpi", "label": "hero-original", "weight": 100,
     "content": {"hero_h1": "The Data Center Power Index",
                 "hero_lede": "Real-time power availability across U.S. data center markets."}},
    {"surface": "/dcpi", "label": "hero-contrarian", "weight": 100,
     "content": {"hero_h1": "Where data center power actually exists.",
                 "hero_lede": "The contrarian metric the incumbents will not publish."}},
    # CTA copy A/B
    {"surface": "/dcpi/cta", "label": "cta-upgrade", "weight": 100,
     "content": {"cta_text": "Upgrade to Pro - $199/mo"}},
    {"surface": "/dcpi/cta", "label": "cta-county", "weight": 100,
     "content": {"cta_text": "Unlock county-level scoring"}},
]

for v in VARIANTS:
    body = json.dumps(v).encode("utf-8")
    req = urllib.request.Request(
        f"{API}/api/v1/variants",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            print(f"  [{r.status}] {v["surface"]}/{v["label"]}")
    except Exception as e:
        print(f"  [error] {v["label"]}: {e}")
