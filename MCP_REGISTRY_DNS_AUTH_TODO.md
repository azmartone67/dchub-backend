# Official MCP Registry — DNS Auth Setup

> 🔐 **SECURITY (2026-05-30):** The previous private key was committed to this
> PUBLIC repo and is therefore **compromised**. It has been **rotated**. The new
> private key lives ONLY at `~/.mcp-publisher/dchub-registry-key.hex` (mode 600,
> outside any repo) and must **never** be pasted into a tracked file again.
> Old (dead) public key was `/hgVylyu8...HOe8=`; current is below.

## Step 1 — Apex TXT record (not a subdomain)

⚠️ The TXT record goes on the **apex domain** (`dchub.cloud`), NOT a subdomain.
MCP DNS auth uses the apex like SPF, not a selector like DKIM. Apex-vs-selector
confusion is the #1 cause of "no MCP public key found" errors.

Cloudflare → DNS records:
**https://dash.cloudflare.com/4bb33ec40ef02f9f4b41dc97668d5a52/dchub.cloud/dns/records**

| Field | Value |
|---|---|
| Type | `TXT` |
| Name | `@` (apex — shown as `dchub.cloud`) |
| Content | `v=MCPv1; k=ed25519; p=ClgJ51i8YWYU+UtKJlz4H3owY44Dhnr3jGLVH1VXAgc=` |
| TTL | Auto |
| Proxy | DNS only |

Replace the OLD `v=MCPv1; ... p=/hgVylyu8...` record's content with the value above
(edit the existing record, or delete it and add this one). `p=` is base64.

## Step 2 — Wait ~60s, flush DNS cache, verify

```bash
sudo dscacheutil -flushcache && sudo killall -HUP mDNSResponder
dig +short TXT dchub.cloud | grep -i mcpv1
# expect the ClgJ51i8... value
```

## Step 3 — Log in + publish (private key read from the secure file, never inline)

```bash
cd /Users/jonathanmartone/dchub-backend
mcp-publisher login dns --domain dchub.cloud \
  --private-key "$(cat ~/.mcp-publisher/dchub-registry-key.hex)"
mcp-publisher publish
```

## Step 4 — Verify live

```bash
curl -s "https://registry.modelcontextprotocol.io/v0/servers?search=dchub" \
 | python3 -c "import json,sys;[print(x['server']['name'],'v'+x['server']['version']) for x in json.load(sys.stdin)['servers'] if x['server']['name']=='cloud.dchub/mcp-server']"
```

## Reference
- mcp-publisher CLI: `/usr/local/bin/mcp-publisher`
- Key generation (ed25519): `openssl genpkey -algorithm ed25519` →
  seed hex = `openssl pkey -outform DER | tail -c 32 | xxd -p -c 64`;
  pub b64 = `openssl pkey -pubout -outform DER | tail -c 32 | base64`
- Registry DNS handler: https://github.com/modelcontextprotocol/registry/blob/main/internal/api/handlers/v0/auth/dns.go
