#!/usr/bin/env python3
"""Seed initial A/B variants for /dcpi. Idempotent. Phase 127A: proper UA."""
import os, json, urllib.request

API = os.environ.get("DCHUB_API_BASE", "https://dchub.cloud")

VARIANTS = [
    {"surface": "/dcpi", "label": "hero-original", "weight": 100,
     "content": {"hero_h1": "The Data Center Power Index",
                 "hero_lede": "Real-time power availability across U.S. data center markets."}},
    {"surface": "/dcpi", "label": "hero-contrarian", "weight": 100,
     "content": {"hero_h1": "Where data center power actually exists.",
                 "hero_lede": "The contrarian metric the incumbents will not publish."}},
    {"surface": "/dcpi/cta", "label": "cta-upgrade", "weight": 100,
     "content": {"cta_text": "Upgrade to Pro - $199/mo"}},
    {"surface": "/dcpi/cta", "label": "cta-county", "weight": 100,
     "content": {"cta_text": "Unlock county-level scoring"}},
]

for v in VARIANTS:
    body = json.dumps(v).encode("utf-8")
    req = urllib.request.Request(
        API + "/api/v1/variants",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; DCHub-Seed/1.0; +https://dchub.cloud)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            label = v["label"]
            surface = v["surface"]
            print("  [{0}] {1}/{2}".format(r.status, surface, label))
    except Exception as e:
        print("  [error] {0}: {1}".format(v["label"], e))
