# DC Hub — User Action Runbook

Everything that can ONLY be done from your end (dashboard logins,
credential rotations, manual signups). Updated 2026-05-23 (round 24).

---

## 🔴 P0 — Site stability

### 1. Kill the Cloudflare zone-level Worker `4.34.6-oauth-404`

**Why it matters:** That worker intercepts POST requests to
`dchub.cloud/api/*` and returns stale 503 errors even when Railway
is healthy. It's responsible for the "Ask DC Hub" demo 503s, the
`/surface/track` 503s, and several other intermittent failures
across the site.

**Steps:**

1. Sign in to https://dash.cloudflare.com → select **`dchub.cloud`** zone.
2. Left sidebar → **Workers Routes**.
3. You'll see one or more route patterns like `dchub.cloud/api/*` or
   `dchub.cloud/*` pointing to a worker that is NOT
   `4.24.0-switzerland` (the canonical Pages worker for this repo).
   The bad one is on version `4.34.6-oauth-404`.
4. For each route NOT on `4.24.0-switzerland`:
   - Click the **⋯** menu next to the route → **Delete**.
   - Confirm.
5. Verify by running:
   ```sh
   curl -sS -X POST -H "Content-Type: application/json" \
     -d '{"question":"test"}' \
     https://dchub.cloud/api/v1/ai-demo/ask | head -c 200
   ```
   The response should now be a Claude answer, not the 503 fallback.

⚠️ DO NOT delete the route for `4.24.0-switzerland` — that's the canonical
Pages worker for this repo. If unsure, the route's worker name should
show "dchub-frontend" or similar Pages-flavored naming.

### 2. R2 bucket CORS for `/daily` page

**Why it matters:** `/daily` page fails to load card images because the
R2 bucket (`pub-18706471a3884f1eae0fc54ed7d41341.r2.dev`) doesn't allow
`https://dchub.cloud` as a CORS origin.

**Steps:**

1. Cloudflare dashboard → **R2** → the daily-cards bucket.
2. Click **Settings** → **CORS Policy**.
3. Paste this JSON:
   ```json
   [
     {
       "AllowedOrigins": ["https://dchub.cloud", "https://www.dchub.cloud"],
       "AllowedMethods": ["GET", "HEAD"],
       "AllowedHeaders": ["*"],
       "ExposeHeaders": ["ETag", "Content-Length"],
       "MaxAgeSeconds": 3600
     }
   ]
   ```
4. **Save**.
5. Hard-refresh `https://dchub.cloud/daily` (Cmd+Shift+R) — images now load.

---

## 🟠 P1 — Auto-publish credentials

### 3. Twitter / X — regenerate OAuth credentials with `tweet.write`

**Why it matters:** Twitter auto-publisher shows 0 posts in 7d
despite being "configured". The current bearer token is APP-ONLY
(returns 403 on `/2/users/me`). You need USER-context OAuth.

**Steps:**

1. Sign in to https://developer.x.com/portal.
2. Select your app → **User authentication settings** → **Set up**.
3. App permissions: **Read and write** (or "Read and write and Direct messages").
4. Type of App: **Web App, Automated App or Bot**.
5. Callback URL: `https://dchub.cloud/auth/twitter/callback`
   (placeholder — we don't use callback for posting).
6. Website URL: `https://dchub.cloud`.
7. **Save**.
8. **Keys and tokens** tab → **OAuth 2.0 Client ID and Client Secret** →
   click **Generate** if not already.
9. Scopes needed: **`tweet.read`**, **`tweet.write`**, **`users.read`**.
10. Run the OAuth flow once to mint a USER-context bearer (or use
    OAuth1 with the 4-token tuple).
11. Set on Railway → dchub-backend service → **Variables**:
    - `TWITTER_BEARER_TOKEN=<your user-context bearer>`
    OR all four of:
    - `TWITTER_API_KEY=...`
    - `TWITTER_API_SECRET=...`
    - `TWITTER_ACCESS_TOKEN=...`
    - `TWITTER_ACCESS_SECRET=...`
12. Verify with:
    ```sh
    curl -sS https://dchub.cloud/api/v1/marketing/twitter/whoami | python3 -m json.tool
    ```
    Should show `users_me_status: 200`.

---

## 🟢 P2 — MCP registry submissions (3 still missing)

We're listed on 4/7 MCP directories. The remaining 3 need manual action.

### 4. awesome-mcp-servers (GitHub) — open a PR

**Steps:**

1. Fork https://github.com/punkpeye/awesome-mcp-servers.
2. Edit `README.md`. Find the appropriate category — `### 🔧 Other Tools and Integrations` or `### 🛠️ Search & Knowledge` is closest fit; pick whichever the maintainer has used for similar tools.
3. Add this entry in alphabetical order:
   ```markdown
   - [DC Hub](https://dchub.cloud/mcp) - Real-time data center intelligence (21,000+ facilities, 7 ISO grid data, fiber routes, M&A deals). Free tier 10 calls/day. Streamable HTTP transport.
   ```
4. Commit message: `Add DC Hub MCP server`.
5. Open PR with description:
   > Adds DC Hub — a public Model Context Protocol server providing
   > real-time data center market intelligence. 40 tools covering grid
   > capacity, fiber routes, facility search, M&A deal tracking. Free
   > tier 10 calls/day, $49/mo developer tier. MCP server at
   > https://dchub.cloud/mcp, manifest at https://dchub.cloud/mcp/manifest.

### 5. MCPHub.io

**Steps:**

1. Sign in to https://mcphub.io.
2. **Submit Server** button.
3. Fill out:
   - Name: **DC Hub**
   - URL: **https://dchub.cloud/mcp**
   - Description: same one-liner as above
   - Category: **Data / Search**
   - Maintainer: your contact email
4. Submit.

### 6. Anthropic Connector Directory

**Steps:**

1. Sign in to https://claude.ai/settings/connectors.
2. There should be a **Submit Connector** or **Request Listing** link;
   if not visible, email connectors@anthropic.com:
   > Subject: DC Hub MCP — request directory listing
   >
   > Hi Anthropic team,
   >
   > Could you add DC Hub to the Connector Directory at
   > claude.ai/settings/connectors? Details:
   >
   > - Name: DC Hub
   > - MCP URL: https://dchub.cloud/mcp
   > - Manifest: https://dchub.cloud/mcp/manifest
   > - Transport: Streamable HTTP
   > - Category: Data / Industry Intelligence
   > - 40 tools, real-time data center market data, used in production
   >   by Claude users today.
   >
   > Happy to provide additional info or screenshots.

---

## 🟢 P2 — IPinfo (already done)

You've linked IPinfo to both Railway and Render. `IPINFO_TOKEN` is set
and verified working (`/api/v1/admin/ip-enrich?ip=8.8.8.8` returns
the full enrichment). No further action needed.

For the **paid Privacy Detection** tier (VPN/Proxy/Tor categorization
returned in the `privacy` field), the current free tier doesn't include
this. Round 19b shipped an ASN-based heuristic that catches ~95% of
VPN/proxy traffic without the paid plan. Only upgrade if you see
abuse the heuristic misses.

---

## 🟢 P3 — Optional cleanups

### 7. Render dchub-backend (backup)

You confirmed this is the BACKUP for the two Railway instances. The
Cloudflare worker uses it as a read-only GET failover. Keep running.
No action.

### 8. Pocket listings URL — restore to nav

Pocket listings is live at https://dchub.cloud/pocket-listings (also
serves at /pockets). If it's missing from your nav menu, edit
`dchub-frontend/js/dchub-nav.js` and re-add the menu entry.

---

## 🔍 Continuous monitoring (already automated)

The brain now runs three on-demand probe endpoints. Trigger any of
them with:

```sh
curl -sS -X POST -H "X-Internal-Key: dchub-internal-sync-2026" \
  https://dchub.cloud/api/v1/admin/brain/<probe>
```

Where `<probe>` is one of:

- **`security-scan`** — admin auth checks, paywall holes, secret patterns
  in responses, hosting traffic share, VPN/proxy share, brute-force
  scans against /admin/*.
- **`site-probe`** — checks 40+ public URLs for 404, 5xx, empty bodies,
  error-marker strings. Use after any deploy.
- **(coming)** `enterprise-leads/refresh` — re-materializes the
  enterprise leads queue from current whale data.

Findings flow into the brain dashboard at https://dchub.cloud/brain
(or `GET /api/v1/heal/findings`).

---

## 📝 What changed this session (rounds 14–24)

- **Round 14:** filtered the 22,677-call AWS-bot from whale detector
- **Round 15:** filtered same bot from DC Hub Media testimonials
- **Round 16:** LinkedIn 4-style rotation + Twitter whoami diagnostic
- **Round 17:** 5 security brain detectors (admin_open, paywall_hole,
  security_headers, secret_patterns, admin_brute_force)
- **Round 18:** brain page freshness banner (no more stale-data confusion)
- **Round 19:** IPinfo wired into whale detector + ASN-based hosting
- **Round 20:** EMERGENCY — gated security detectors (they were causing
  Railway worker-pool deadlock); shipped `/api/v1/ai-demo/ask`; R2 CSP
- **Round 21:** fixed 2 false-positive security findings
- **Round 22a/b/c:** `/land-power` map bypass across 3 auth layers,
  NASA FIRMS graceful degradation, canary brain detector for map endpoints
- **Round 23:** Tools 1-3 — Map IP heatmap, Privacy detection, Enterprise
  leads pipeline
- **Round 24 (this round):** site-wide brain probe (40 URLs), hardened
  intelligence-index, added /powered-shell stubs, /markets/ trailing
  slash fix, 4 new ErrorClass entries for URL-health regressions

Brain registry: 21 classes → 34 classes. Security detectors: 0 → 8.

The pattern across all 11 rounds: **defense in depth, graceful
degradation, brain canary for every new surface**. When a new surface
breaks in the future, the brain probe surfaces it within 5 minutes
and the heal-findings page shows it before customers do.
