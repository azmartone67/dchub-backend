# DC Hub — Step-by-Step Action Guide

Maximally detailed walkthroughs for the four pending manual items.
Each section is self-contained — print it, screenshot it, or work
through it click-by-click. Time estimates included.

Date: 2026-05-23 (round 25). Pairs with `USER_ACTIONS.md`.

---

## 🔴 Item 1: Delete the rogue Cloudflare zone worker `4.34.6-oauth-404`

**Time:** 3-5 minutes
**Why:** This worker intercepts POST requests to `dchub.cloud/api/*`
and returns stale 503s even when Railway is healthy. Confirmed cause
of the Ask DC Hub demo failures, `/surface/track` 503s, and some
`/api/agents/intelligence-index` errors today.

### Steps

1. Open https://dash.cloudflare.com in a browser. Sign in.

2. From your account home, click the **`dchub.cloud`** domain card.
   This drops you into the zone-level dashboard for that domain.

3. In the left sidebar, scroll down to the **Workers** section. Click
   **Workers Routes**.
   - URL bar should now show `https://dash.cloudflare.com/<account_id>/dchub.cloud/workers`

4. You'll see a list of routes. Each row has:
   - **Route pattern** (e.g. `dchub.cloud/api/*`)
   - **Worker** (the worker script name)
   - **Failure mode** (Fail open / Fail closed)
   - **⋮** action menu

5. **Identify the rogue worker.** Look for any row where the worker
   name is NOT one of these (these are the GOOD ones):
   - `dchub-frontend` (or a name matching your Cloudflare Pages project)
   - The canonical Pages worker — version reported as `4.24.0-switzerland`
     by `curl https://dchub.cloud/api/v1/version | jq .version`

   The rogue worker shows up with versions like:
   - `4.34.6-oauth-404` (OAuth zone worker — intercepts /api/*)
   - `4.8.5-mcp-landing` (MCP zone worker — intercepts /mcp/*)
   - Sometimes a date-style name or "[deleted]"

6. **For each rogue route**, click the **⋮** menu → **Delete route**.
   - Cloudflare will ask "Are you sure?" — confirm.
   - The route is deleted within ~30 seconds.

7. **Verify the fix:**
   ```sh
   # All three should return 200, not 503:
   curl -sS -X POST -H "Content-Type: application/json" \
     -d '{"question":"test"}' \
     https://dchub.cloud/api/v1/ai-demo/ask | head -c 200

   curl -sS -X POST -H "Content-Type: application/json" \
     -d '{"surface":"x","event":"y"}' \
     https://dchub.cloud/api/v1/surface/track

   curl -sS https://dchub.cloud/api/v1/version | jq .version
   # Expected: "4.24.0-switzerland"
   ```

8. **Also check `/mcp/manifest`:**
   ```sh
   curl -sS https://dchub.cloud/mcp/manifest | head -c 200
   ```
   If you see `<pre>Cannot GET /mcp/manifest</pre>`, the
   `4.8.5-mcp-landing` zone worker is still alive — go back to step 5
   and delete that route too.

⚠️ **DO NOT delete the route for the Pages worker** (`4.24.0-switzerland`
or whatever your Pages project deploys). That's the canonical Pages
worker for this repo. If unsure, the Pages worker name typically
matches your CF Pages project name (e.g. `dchub-frontend`).

### What if you accidentally delete the Pages worker?

You can re-bind it: CF dashboard → **Pages** → your project → **Custom domains**
→ add `dchub.cloud` back. CF auto-creates the route. ~2 minutes.

---

## 🟠 Item 2: Twitter OAuth — regenerate with `tweet.write` scope

**Time:** 15-25 minutes
**Why:** Twitter auto-publisher shows 0 posts in 7d despite 11
approved Tweets queued. Diagnosed via:

```sh
curl https://dchub.cloud/api/v1/marketing/twitter/whoami | jq .diagnosis
```

Returns: *"Bearer token returns 403 — token lacks 'tweet.write'
scope. App-only bearer cannot post tweets."*

### Steps

1. Open https://developer.x.com/portal/projects-and-apps. Sign in
   with the X account that owns @dchubcloud (or your bot account).

2. **Select your app** under your Project. If no app exists, click
   **+ Add App** and name it "DC Hub Auto-Publisher".

3. Click your app → **Settings** tab → scroll to **User authentication
   settings** → **Set up** button.

4. **App permissions:** select **Read and write**.
   *(For DMs too, pick "Read and write and Direct messages" instead.)*

5. **Type of App:** select **Web App, Automated App or Bot**.

6. **App info:**
   - Callback URI / Redirect URL:
     `https://dchub.cloud/auth/twitter/callback`
     (placeholder — we don't actually use OAuth callback, but X
     requires the field).
   - Website URL: `https://dchub.cloud`
   - Organization name: `DC Hub`
   - Organization URL: `https://dchub.cloud`

7. Click **Save**. You'll see a popup with a Client ID and Client
   Secret. **Copy both** into a password manager.

8. Click **Keys and tokens** tab.
   - Under **OAuth 2.0 Client ID and Client Secret**, ensure they're
     generated (regenerate if needed).
   - Under **Authentication Tokens**:
     - **Access Token and Secret** — click **Regenerate**. Copy both.
     - **Bearer Token** — click **Regenerate**. Copy.

9. **Choose your auth strategy** (pick ONE):

   ### Strategy A — OAuth 1.0a User Context (simpler for bots)
   You'll need 4 values from step 8:
   - API Key (from app's "Consumer Keys" section)
   - API Key Secret (same section)
   - Access Token (Authentication Tokens section)
   - Access Token Secret (same section)

   On Railway → `dchub-backend` service → **Variables** tab → click
   **+ New Variable** four times, paste each:
   ```
   TWITTER_API_KEY=...
   TWITTER_API_SECRET=...
   TWITTER_ACCESS_TOKEN=...
   TWITTER_ACCESS_SECRET=...
   ```
   Click **Deploy**. Railway will auto-restart with new env vars.

   ### Strategy B — OAuth 2.0 User Context Bearer (newer X API)
   Run this OAuth flow once to mint a user-context bearer
   (more involved — requires a small Python script). Skip to
   Strategy A unless you specifically need OAuth 2.0.

10. **Verify auth works** (~2 min after Railway restart):
    ```sh
    curl -sS https://dchub.cloud/api/v1/marketing/twitter/whoami | jq .
    ```
    Expected:
    ```json
    {
      "ok": true,
      "users_me_status": 200,
      "masked": { "oauth1_complete": true },
      "queue_14d": { "approved": N, "published": M },
      "diagnosis": []
    }
    ```
    If diagnosis is empty + users_me_status is 200, you're set.

11. **Force-flush the queue** (optional, immediate publish):
    ```sh
    curl -sS -X POST \
      -H "X-Internal-Key: dchub-internal-sync-2026" \
      "https://dchub.cloud/api/v1/marketing/publish-now?only=twitter"
    ```

12. **Verify on Twitter** — open `https://twitter.com/dchubcloud` and
    you'll see the most-recent press release auto-posted within 6
    hours (the cron interval). Force-flush above bypasses the wait.

---

## 🟢 Item 3: MCP registry manual submissions (3 remaining)

**Time:** ~10 min for awesome-mcp-servers PR + 5 min each for the
other two = ~20 min total.

You're listed on Smithery + mcp.so + Glama + PulseMCP. The 3 missing
are below.

### 3a. awesome-mcp-servers (GitHub) — open a PR

1. Open https://github.com/punkpeye/awesome-mcp-servers in a browser.
   Sign into GitHub.

2. Click **Fork** (top right). Wait ~5s for the fork to complete.
   You're now on `<your-username>/awesome-mcp-servers`.

3. Click the **README.md** file → pencil **edit** icon (top right of
   the file view).

4. Use Cmd/Ctrl+F to find a section heading like:
   - `### 🔍 Browser Automation` — wrong category
   - `### 🛠️ Search & Web` — could fit
   - `### 📂 Filesystem` — wrong
   - `### 🏢 Enterprise & Business` — best fit OR
   - `### 📚 Knowledge & Memory` — also fits

   Pick **`### 🏢 Enterprise & Business`** if it exists, otherwise the
   most-similar one. Find the closing bullet of that section.

5. Insert this new line in alphabetical order within the section:
   ```markdown
   - [DC Hub](https://dchub.cloud/mcp) - Real-time data center intelligence: 21,000+ facilities, 7 ISO grid data, fiber routes, M&A deals, capacity pipeline. Free tier 10 calls/day. Streamable HTTP transport.
   ```

6. Scroll to the bottom of the edit form. Under **Commit changes**:
   - Commit message: `Add DC Hub MCP server`
   - Extended description: leave blank
   - Select **Create a new branch and start a pull request**
   - Branch name: `add-dc-hub`
   - Click **Propose changes**.

7. On the PR creation page:
   - Title: `Add DC Hub — real-time data center intelligence MCP`
   - Description: copy-paste this:
     ```markdown
     ## Server: DC Hub
     - URL: https://dchub.cloud/mcp
     - Transport: Streamable HTTP (Model Context Protocol)
     - Manifest: https://dchub.cloud/mcp/manifest
     - Tools: 40 (facility search, grid data, fiber routes,
       M&A deals, capacity pipeline, AI integration index)
     - Tier: Free 10 calls/day, $49/mo developer (1000 calls/day),
       $199/mo pro (10k/day), $499/mo enterprise (100k/day)
     - Production: serving Claude / ChatGPT / Gemini / Copilot users today

     Tested with Claude Desktop, Claude.ai connector,
     Smithery, and mcp.so. Also listed on Glama AI + PulseMCP.
     ```
   - Click **Create pull request**.

8. The maintainer (punkpeye / community) will review. Expect 1-7 day
   merge time. You'll get GitHub notification on merge.

### 3b. MCPHub.io

1. Open https://mcphub.io. Sign up if needed (GitHub OAuth works).

2. Look for a **Submit Server** button — usually top-right header
   or a button on the homepage.

3. Form fields (paste):
   - **Name:** `DC Hub`
   - **URL / Server endpoint:** `https://dchub.cloud/mcp`
   - **Manifest URL:** `https://dchub.cloud/mcp/manifest`
     *(if /mcp/manifest still 404s because of the zone worker —
     finish Item 1 first)*
   - **Description:**
     ```
     Real-time data center intelligence: 21,000+ facilities, 7
     ISO grid data, fiber routes, M&A deals, capacity pipeline.
     Used by Claude, ChatGPT, Gemini users in production.
     Free 10 calls/day, paid from $49/mo.
     ```
   - **Category:** `Data` or `Industry Intelligence` (whichever exists)
   - **Tags:** `data center`, `infrastructure`, `grid`, `fiber`, `m&a`
   - **Maintainer:** your email (e.g. `azmartone@gmail.com`)
   - **Documentation:** `https://dchub.cloud/integrations/mcp`

4. Click **Submit**. Audit time is ~24-72h.

### 3c. Anthropic Connector Directory

1. Open https://claude.ai/settings/connectors.

2. Look for a **Submit Connector** or **Request a connector** link.
   If not visible (most users don't see it — it's a controlled
   directory), email connectors@anthropic.com:

   **Subject:** `DC Hub MCP — request directory listing`

   **Body** (paste):
   ```
   Hi Anthropic team,

   Could you add DC Hub to the Connector Directory at
   claude.ai/settings/connectors? Details:

   - Name: DC Hub
   - MCP URL: https://dchub.cloud/mcp
   - Manifest: https://dchub.cloud/mcp/manifest
   - Transport: Streamable HTTP (MCP 2024-11-05 and 2025-06-18)
   - Category: Data / Industry Intelligence
   - Tools: 40 (facility search, grid data, fiber routes, M&A deals,
     capacity pipeline, AI agent intelligence index)
   - Pricing: Free 10 calls/day, $49/mo (1000/day), $199/mo (10k/day),
     $499/mo (100k/day)
   - Production: Already serving Claude users via direct connection
     today; 20,939 MCP tool calls in last 7 days.

   We're listed on Smithery (azmartone67/dchub), mcp.so (/server/dc-hub),
   Glama AI, and PulseMCP already. Anthropic Directory listing would
   help discoverability for Claude.ai users.

   Happy to provide additional info, screenshots, or a demo.
   Test the server now with: 
     curl -X POST https://dchub.cloud/mcp -H "Content-Type: application/json" \
       -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{
            "protocolVersion":"2024-11-05","capabilities":{},
            "clientInfo":{"name":"audit","version":"1"}}}'

   Thanks,
   Jonathan Martone
   DC Hub
   ```

3. Send. Typical response: 1-2 weeks.

### 3d. Trigger the autonomous registry-resubmit cron (BONUS)

After round 25, the submit-all endpoint accepts the legacy key. Run:

```sh
curl -sS -X POST -H "X-Internal-Key: dchub-internal-sync-2026" \
  "https://dchub.cloud/api/v1/admin/outreach/mcp-registry/submit-all" \
  | jq '.[] | {target, audit_listed: .audit.listed, http: .audit.http_code}'
```

Output shows current listed/unlisted status per target. The cron
re-submits to Smithery / Glama / PulseMCP (they refresh from
our public manifest); for MCPHub + awesome-mcp + Anthropic, it
just records a "queued" entry because submission is manual (above).

---

## 🟢 Item 4: Deploy `dchub-frontend/visitor-map.html` to Cloudflare Pages

**Time:** 1-3 minutes (depends on how your Pages deploy is configured)

### If you have CF Pages auto-deploy from GitHub (most likely)

Just push the latest commits — round 23 already added
`visitor-map.html` and round 24c added `/visitor-map` to nav.

```sh
cd /Users/jonathanmartone/dchub-backend
git pull --rebase    # ensure you have my latest commits
git push             # if you had local changes
```

Cloudflare Pages will auto-build within ~60 seconds. Watch the deploy
status at:
```
https://dash.cloudflare.com/<account>/pages/view/dchub-frontend
```

After deploy completes:
```sh
curl -sSI https://dchub.cloud/visitor-map | head -1
# Expected: HTTP/2 200
```

Visit `https://dchub.cloud/visitor-map` in a browser — you should see
the dark heatmap UI with the legend. Initial view defaults to "real
customers only" (filters out AWS/GCP bots).

### If CF Pages does NOT auto-deploy

1. CF dashboard → **Pages** → your Pages project (probably named
   `dchub-frontend`).
2. **Settings** → **Builds & deployments** → **Production branch**:
   confirm it's `main` (or whatever branch you push to).
3. **Build configuration** → confirm **Build output directory** is
   `dchub-frontend/` (or whatever directory holds your HTML files).
4. Trigger a manual deploy: top right → **Create deployment** →
   pick branch → **Save and Deploy**.

### Verify

```sh
# Page renders:
curl -sS https://dchub.cloud/visitor-map | grep -c "DC Hub · Visitor Map"
# Expected: 1

# API works (no auth needed):
curl -sS https://dchub.cloud/api/v1/visitor-map?exclude_hosting=true | jq .ok
# Expected: true
```

---

## 📋 What if something goes wrong?

For ALL of these, the brain probe will catch regressions within 5 min
once enabled:

```sh
# Run on-demand site probe (40+ URL canary):
curl -sS -X POST -H "X-Internal-Key: dchub-internal-sync-2026" \
  https://dchub.cloud/api/v1/admin/brain/site-probe | jq

# Run security scan (admin endpoints, paywall holes, secret leaks):
curl -sS -X POST -H "X-Internal-Key: dchub-internal-sync-2026" \
  https://dchub.cloud/api/v1/admin/brain/security-scan | jq

# Twitter publisher diagnostic:
curl -sS https://dchub.cloud/api/v1/marketing/twitter/whoami | jq

# Distribution health (LinkedIn / Twitter / Bluesky):
curl -sS https://dchub.cloud/api/v1/marketing/distribution/health | jq
```

All four are documented in `USER_ACTIONS.md`. Brain dashboard at
`https://dchub.cloud/brain` shows live findings.

---

## 🎯 Summary table

| Item | Time | Done? |
|------|------|-------|
| 1. Delete CF zone worker 4.34.6 | 3-5 min | ☐ |
| 1b. Delete CF zone worker 4.8.5 (`/mcp/manifest`) | 3-5 min | ☐ |
| 2. Twitter OAuth regen with tweet.write | 15-25 min | ☐ |
| 3a. awesome-mcp-servers PR | 10 min | ☐ |
| 3b. MCPHub submission | 5 min | ☐ |
| 3c. Anthropic Connector email | 5 min | ☐ |
| 4. Push to deploy visitor-map.html | 1-3 min | ☐ |

**Total time, all four items:** 45-60 minutes.

If you do them in this order (1 → 4 → 2 → 3), each step unblocks
verification of the next, and you can confirm at every stage instead
of waiting until the end.
