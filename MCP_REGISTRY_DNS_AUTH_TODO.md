# Official MCP Registry — DNS Auth Setup (~3 min total)

## Step 1 — Add the TXT record at the APEX (not a subdomain)

⚠️ **Critical:** the TXT record goes on the **apex domain** (`dchub.cloud`), NOT a subdomain like `_mcp-publisher`. MCP DNS auth uses the apex like SPF, not a selector like DKIM. The registry source code has a comment explicitly warning about this — apparently it's the #1 cause of "no MCP public key found" errors.

Open your already-logged-in Cloudflare tab and go to:
**https://dash.cloudflare.com/4bb33ec40ef02f9f4b41dc97668d5a52/dchub.cloud/dns/records**

Click **Add record** and fill in:

| Field | Value |
|---|---|
| Type | `TXT` |
| Name | `@` (apex — Cloudflare displays this as just `dchub.cloud`) |
| Content | `v=MCPv1; k=ed25519; p=/hgVylyu8smrR9gCL5w6eno6Hn8Dav0DA9WYDhOHOe8=` |
| TTL | Auto |
| Proxy status | DNS only (TXT records aren't proxiable anyway) |

Click **Save**.

If you already created the `_mcp-publisher.dchub.cloud` TXT record, you can **delete it** — it's harmless but unused.

**About the value:** the `p=` part is the public key in **base64**, not hex. The private key in Step 3 is still **hex** — same key, two encodings.

## Step 2 — Wait ~60 sec, flush local DNS cache, then verify

macOS aggressively caches DNS. Flush so the next `dig` is fresh:

```bash
sudo dscacheutil -flushcache && sudo killall -HUP mDNSResponder
dig +short TXT dchub.cloud | grep -i mcpv1
# expect: "v=MCPv1; k=ed25519; p=/hgVylyu8smr..."
```

## Step 3 — Log in + publish

```bash
cd /Users/jonathanmartone/dchub-backend
mcp-publisher login dns \
  --domain dchub.cloud \
  --private-key 410f61cdcbbc456dc43bfa8a7646f44b82cf7a9851b1e5351047b32ab5361259

mcp-publisher publish
```

Expected output:
```
Publishing to https://registry.modelcontextprotocol.io...
✓ Successfully published cloud.dchub/mcp-server v2.1.10
```

## Step 4 — Verify the update is live

```bash
curl -s "https://registry.modelcontextprotocol.io/v0/servers?search=dchub" | python3 -c "import json,sys; s=json.load(sys.stdin)['servers'][0]['server']; print(s['name'], 'v'+s['version']); print(s['description'][:120])"
```

Expected:
```
cloud.dchub/mcp-server v2.1.10
Data center intelligence: 21,000+ facilities, 10 ISO grids, M&A, interconnection queue, AI capacity.
```

## What this updates

The existing `cloud.dchub/mcp-server` listing on the Official MCP Registry — bumps from v1.0.0 (March 2026, "20K facilities") to v2.1.10 ("21K facilities, 10 ISO grids, interconnection queue, AI capacity"). The remote URL `https://dchub.cloud/mcp` is already correct.

**Downstream effect:** PulseMCP (16K-server directory) auto-ingests from the Official MCP Registry weekly. Within ~7 days of this publish, your PulseMCP listing also refreshes to v2.1.10 for free.

## Reference

- mcp-publisher CLI: `/usr/local/bin/mcp-publisher`
- Registry DNS handler source: https://github.com/modelcontextprotocol/registry/blob/main/internal/api/handlers/v0/auth/dns.go
- Apex-vs-selector confusion: see issues [#385](https://github.com/modelcontextprotocol/registry/issues/385), [#1103](https://github.com/modelcontextprotocol/registry/issues/1103), [#1126](https://github.com/modelcontextprotocol/registry/issues/1126)
