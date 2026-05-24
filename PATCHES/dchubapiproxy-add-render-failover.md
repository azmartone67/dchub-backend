# Patch: `dchubapiproxy` worker — add Render failover (STEP 2.5)

**Why:** `dchub.cloud/api/*` (Pages worker `dchub-frontend` v4.24.0-switzerland) has the chain Railway → **Render** → KV stale → 503. But `api.dchub.cloud/api/*` (`dchubapiproxy` v4.8.5-mcp-landing) is missing the Render step — it goes Railway → KV stale → 503 directly. When Railway is overloaded, api.dchub.cloud 503s while dchub.cloud quietly serves from Render.

**Fix:** add a STEP 2.5 that tries Render before falling through to stale KV.

---

## Step-by-step

1. Open https://dash.cloudflare.com → `dchub.cloud` account → **Workers & Pages** → **dchubapiproxy** → **Edit code**.

2. **Add a Render backend constant** near the top of the file (right after `const RAILWAY_BACKEND = ...`):

```javascript
// Phase ZZZZZ-round26 (2026-05-23): Render is the read-only failover
// for GETs. Matches the dchub-frontend Pages worker v4.24.0-switzerland
// failover chain so api.dchub.cloud has the same resilience as
// dchub.cloud when Railway is overloaded.
const RENDER_BACKEND = 'https://dchub-backend-render.onrender.com';
```

3. **Add a `proxyToRender` helper** right below the existing `proxyToRailway` function (around the same place — search for `async function proxyToRailway`):

```javascript
async function proxyToRender(request, pathname, search, timeoutMs) {
  // Render is GET-only (IS_FAILOVER=true on that deploy). POSTs would
  // mutate state and we don't want dual-write — so skip non-GETs.
  if (request.method !== 'GET') return null;
  const targetUrl = RENDER_BACKEND + pathname + (search || '');
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const headers = new Headers(request.headers);
    headers.set('X-Forwarded-Host', 'dchub.cloud');
    headers.set('X-Forwarded-Proto', 'https');
    headers.set('Referer', 'https://dchub.cloud');
    headers.set('Accept-Encoding', 'identity');
    headers.set('X-Failover-Source', 'dchubapiproxy-render');
    const resp = await fetch(targetUrl, {
      method: 'GET',
      headers,
      signal: controller.signal,
      redirect: 'manual',
    });
    clearTimeout(timer);
    return resp;
  } catch (e) {
    clearTimeout(timer);
    return null;
  }
}
```

4. **Find STEP 2** (search for `// STEP 2: Proxy to Railway` or the `proxyWithRetry` call). After the `if (resp && resp.status < 500) { ... return result; }` block but BEFORE the `// STEP 3: Stale KV` comment, **insert this new STEP 2.5 block**:

```javascript
// STEP 2.5: Render failover (GETs only — Render runs IS_FAILOVER=true
// read-only). Phase ZZZZZ-round26 (2026-05-23): matches the Pages
// worker's resilience pattern so api.dchub.cloud doesn't 503 immediately
// when Railway hiccups.
if (isGet) {
  const renderResp = await proxyToRender(request, pathname, url.search, 45000);
  if (renderResp && renderResp.status < 500) {
    const result = addCORS(new Response(renderResp.body, renderResp), request);
    result.headers.set('x-dc-hub-backend', 'render');
    result.headers.set('x-dc-hub-failover', 'true');
    result.headers.set('X-Failover-Mode', 'render-active');
    result.headers.set('X-DC-Worker-Version', WORKER_VERSION);
    result.headers.set('X-DC-Response-Time', `${Date.now() - startTime}ms`);
    return result;
  }
}
```

5. **Update the 503 fallback message** (find `// STEP 4: 503` then the next `tip:` line). Change:
```javascript
tip: 'This message lands when both Railway and KV stale cache are unavailable.'
```
to:
```javascript
tip: 'This message lands when Railway, Render failover, and KV stale cache are all unavailable.'
```

6. **Bump the WORKER_VERSION constant** so we can confirm the new code is live:
```javascript
const WORKER_VERSION = '4.8.6-render-failover';
```

7. Click **Save and deploy**.

8. Verify the failover works:
```bash
# Force a 503 path. Then check the worker_version in the response.
# After this patch lands, the version should be 4.8.6-render-failover
# AND the tip should mention Render.
curl -sS https://api.dchub.cloud/api/v1/admin/brain/site-probe -X POST \
  -H "Content-Type: application/json" \
  -H "X-Internal-Key: dchub-internal-sync-2026" | python3 -m json.tool
```

9. Also verify GET failover by checking response headers when Railway is healthy:
```bash
curl -sSI https://api.dchub.cloud/api/v1/stats | grep -iE "x-dc-hub-backend|x-failover"
# Should show x-dc-hub-backend: railway (healthy path)
# When Railway is unhealthy, expect: x-dc-hub-backend: render + x-failover-mode: render-active
```

---

## What this fix does NOT do
- Doesn't change POST handling (still goes straight to Railway → 503 if dead; Render is GET-only)
- Doesn't change caching, auth, or any other route
- Render must be running `IS_FAILOVER=true` mode (it already is per user confirmation)
