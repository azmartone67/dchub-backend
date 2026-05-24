# Patch: `mcp-proxy` worker — inline `/mcp/manifest` handler

**Why:** Claude.ai connector dialog probes `/mcp/manifest` to validate the server. Currently the mcp-proxy passes through to `dchub-mcp-server-production-4d2e.up.railway.app/mcp/manifest` which returns 404 (no handler upstream). Result: Claude.ai shows "Couldn't reach the MCP server" even though `POST /mcp` works perfectly.

**Fix:** add a static manifest response IN THE WORKER, before the pass-through, so the manifest is served from the edge without needing an MCP-backend code change.

---

## Step-by-step

1. Open https://dash.cloudflare.com → `dchub.cloud` account → **Workers & Pages** → **mcp-proxy** → **Edit code** (the `<>` icon top-right of the worker page).

2. In the editor, find the section that handles `/mcp/*` requests (look for `fetch(` or the start of the request handler). You'll see code that proxies requests to `MCP_BACKEND`.

3. **AT THE VERY TOP** of the `fetch` handler (right after `const url = new URL(request.url);` if that line exists, or just before the first `if`/`return`), paste this block:

```javascript
// ═══════════════════════════════════════════════════════════════
// Phase ZZZZZ-round26 (2026-05-23) — inline /mcp/manifest handler.
// Claude.ai connector validation probes this path BEFORE connecting.
// Upstream dchub-mcp-server returns 404, so Claude.ai gives up with
// "Couldn't reach the MCP server". Serve from the edge instead.
// ═══════════════════════════════════════════════════════════════
if (url.pathname === '/mcp/manifest' || url.pathname === '/mcp/manifest.json') {
  return new Response(JSON.stringify({
    schema_version: 'mcp-server-card/v1',
    name: 'DC Hub Intelligence',
    version: '2.1.2',
    description: 'Real-time data center intelligence: 21,000+ facilities, 7 ISO grid data, fiber routes, M&A deals, capacity pipeline.',
    url: 'https://dchub.cloud/mcp',
    transport: 'streamable-http',
    protocol_version: '2024-11-05',
    provider: {
      organization: 'DC Hub',
      url: 'https://dchub.cloud',
      contact: 'api@dchub.cloud',
    },
    authentication: {
      type: 'api_key',
      header: 'X-API-Key',
      optional_for: ['free_tier'],
      note: 'Free tier (5 calls/day) requires no auth. Paid tiers add X-API-Key header.',
    },
    capabilities: { tools: { listChanged: true } },
    tools_count: 40,
    tools_endpoint: 'POST /mcp with {"jsonrpc":"2.0","id":1,"method":"tools/list"}',
    pricing: {
      free:       '5 calls/day,    truncated results, 20 tools',
      developer:  '$49/mo  1,000/day, all 40 tools',
      pro:        '$199/mo 10,000/day',
      enterprise: '$499/mo 100,000/day',
    },
    documentation: 'https://dchub.cloud/integrations/mcp',
    signup_url:    'https://dchub.cloud/signup',
  }, null, 2), {
    status: 200,
    headers: {
      'Content-Type': 'application/json; charset=utf-8',
      'Cache-Control': 'public, max-age=3600',
      'Access-Control-Allow-Origin': '*',
      'X-DC-Manifest-Source': 'worker-inline',
    },
  });
}
```

4. Click **Save and deploy** (top right).

5. Verify:
```bash
curl -sS https://dchub.cloud/mcp/manifest | python3 -m json.tool
```
Should return the JSON above, NOT `<pre>Cannot GET /mcp/manifest</pre>`.

6. Now retry the Claude.ai connector dialog — it should validate and connect.

---

## What this fix does NOT do
- Doesn't change the `/mcp` JSON-RPC pass-through behavior (still proxies to MCP backend)
- Doesn't change auth, caching, or any other route
- Manifest content is hardcoded — if tool counts change, update this constant. The brain's `check_site_url_health` will catch drift (manifest is in `_PROBE_LIST`).
