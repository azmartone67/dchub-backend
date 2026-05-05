#!/usr/bin/env python3
"""Synthetic MCP health check. Exits 0 if /mcp returns valid initialize response, 1 otherwise.
Run from cron / GitHub Actions / Cloudflare scheduled worker."""
import requests, sys
URL = "https://dchub.cloud/mcp"
PAYLOAD = {"jsonrpc":"2.0","id":1,"method":"initialize",
           "params":{"protocolVersion":"2025-11-25","capabilities":{},
                     "clientInfo":{"name":"synthetic","version":"0.1"}}}
try:
    r = requests.post(URL,
        headers={"Content-Type":"application/json",
                 "Accept":"application/json, text/event-stream"},
        json=PAYLOAD, timeout=10)
    ok = r.status_code == 200 and "DC Hub Intelligence" in r.text
    print(f"MCP synthetic: HTTP {r.status_code}, ok={ok}")
    sys.exit(0 if ok else 1)
except Exception as e:
    print(f"MCP synthetic: EXCEPTION {e}")
    sys.exit(1)
