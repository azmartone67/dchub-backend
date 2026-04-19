# DC Hub — Deployment Lock & Architecture Map v3.0

**Last Updated:** February 26, 2026
**Version:** 3.0 — Post-RFO Edition
**Author:** Jonathan Martone / Claude
**Status:** LOCKED — Do not modify routing without reading this entire document

---

## CRITICAL: Read Before Changing ANYTHING

On Feb 26, 2026, dchub.cloud was down for ~15 hours because a Worker update misidentified where the frontend HTML is served from. This document exists to prevent that from ever happening again.

**THE GOLDEN RULE:** If you don't know which provider serves a path, CHECK THIS DOCUMENT before making changes.

---

## Architecture Overview

```
User Browser
    │
    ▼
┌─────────────────────────────────────────────────┐
│  Cloudflare (dchub.cloud DNS)                   │
│                                                  │
│  Worker: mcp-proxy                               │
│  Route: dchub.cloud/*                            │
│  Role: Smart router — decides where each         │
│        request goes based on the URL path         │
│                                                  │
│  ┌──────────────┐  ┌─────────────────────────┐   │
│  │ NON-API paths │  │ API + Discovery paths   │   │
│  │ / /ai /news   │  │ /api/* /health /.well-  │   │
│  │ /pricing etc  │  │ known/* /llms.txt etc   │   │
│  └──────┬───────┘  └──────────┬──────────────┘   │
│         │                      │                  │
│         ▼                      ▼                  │
│  ┌──────────────┐  ┌─────────────────────────┐   │
│  │ dchub Worker  │  │ Neon Direct (reads)     │   │
│  │ (frontend)    │  │    ↓ fallback ↓         │   │
│  │ workers.dev   │  │ Railway (primary API)   │   │
│  └──────────────┘  │    ↓ fallback ↓         │   │
│                     │ Replit (failover API)    │   │
│                     └─────────────────────────┘   │
└─────────────────────────────────────────────────┘
```

---

## Provider Map — Who Serves What

### 1. Cloudflare Worker: `dchub` (FRONTEND)

- **URL:** `dchub.azmartone.workers.dev`
- **Route:** `dchub.cloud/llms.txt` (only explicit route, but also the frontend origin)
- **Serves:** ALL static HTML pages (index.html, ai.html, news.html, pricing.html, etc.)
- **Contains:** The full DC Hub frontend — HTML, CSS, inline JS
- **NEVER returns JSON** — always HTML/CSS/JS

**⚠️ WARNING:** This is NOT a Cloudflare Pages project. There is NO Pages origin. The frontend is embedded directly in this Worker's code. If you delete or break this Worker, the entire website goes dark.

### 2. Cloudflare Worker: `mcp-proxy` (ROUTER/PROXY)

- **URL:** `mcp-proxy.azmartone.workers.dev`
- **Route:** `dchub.cloud/*` (catches everything)
- **Current Version:** v3.8.5
- **Role:** Smart router that inspects every request and sends it to the right place:

| Path Pattern | Destination | Method |
|---|---|---|
| `/` `/ai` `/news` `/pricing` `/about` `/markets/*` `/facilities/*` `/locations/*` (any non-API path) | `dchub` frontend Worker | `fetch('https://dchub.azmartone.workers.dev' + path)` |
| `/api/health` `/api/news` `/api/v1/stats` `/api/v1/deals` `/api/ai/tracking` etc. (Neon-direct routes) | Neon PostgreSQL | Direct SQL over HTTP |
| `/api/auth/*` `/api/login` `/api/register` `/api/stripe/*` `/api/v1/land-power/*` | Railway → Replit | `transparentProxyWithFailover()` |
| `/api/admin/*` `/api/subscribe` `/api/webhook` `/api/ai/query` `/api/mcp` | Railway → Replit | `proxyWithFailover()` |
| `/dashboard` | Railway → Replit | `transparentProxyWithFailover()` |
| `/.well-known/mcp.json` `/.well-known/agent.json` `/.well-known/security.txt` | Worker inline | Served directly from Worker code |
| `/openapi.json` `/AGENTS.md` `/llms.txt` `/llms-full.txt` `/robots.txt` | Railway → Replit | `proxyDiscoveryPath()` |
| `/api/agents/registry` `/api/agents/intelligence-index` | Railway → **Replit** | 404 on Railway triggers fallthrough to Replit |

**Key routing logic (line ~636):**
```javascript
const isWorkerRoute = pathname.startsWith('/api/') || pathname === '/health'
  || pathname.startsWith('/ai/') || isBackendHtml(pathname) || isDiscoveryPath(pathname);

// Non-API routes go to frontend Worker
if (!isWorkerRoute) {
  fetch('https://dchub.azmartone.workers.dev' + url.pathname + url.search);
}
```

**⚠️ NEVER change this to `transparentProxyWithFailover()` or `fetch(request)` without understanding why:**
- `transparentProxyWithFailover()` → sends to Railway which returns JSON, not HTML = BROKEN SITE
- `fetch(request)` → tries to fetch from a Pages origin that doesn't exist = 522 error

### 3. Railway (PRIMARY BACKEND)

- **URL:** `https://dchub-backend-production.up.railway.app`
- **GitHub:** `https://github.com/azmartone67/dchub-backend`
- **Plan:** Railway Pro
- **Role:** Primary API backend, runs Flask app
- **Serves:** API JSON responses, discovery files, dynamic dashboard
- **Root `/` returns:** `{"status":"healthy","version":"86.0.0","features":[...]}` — **THIS IS NOT YOUR HOMEPAGE**
- **Auto-deploys:** From GitHub `main` branch on push
- **Background tasks:** News scheduler, auto-approval, facility discovery, cache warming (controlled by `IS_RAILWAY` flag)
- **Timeout in Worker:** 12 seconds

**What Railway does NOT have:**
- `agent_network_effect.py` (agent registry/intelligence-index routes) — these 404 on Railway and fall through to Replit
- Frontend HTML files — Railway is API-only

**Environment variables:** Must mirror Replit secrets (Neon DB URL, Stripe keys, API keys, etc.)

### 4. Replit (FAILOVER BACKEND)

- **URL:** `https://dc-hub-replit-fixedzip--azmartone1.replit.app`
- **Role:** Failover API backend, also runs Flask app
- **Serves:** Same API endpoints as Railway + some additional routes (agent registry, intelligence-index)
- **Background tasks:** DISABLED (controlled by absence of `IS_RAILWAY` env var)
- **Timeout in Worker:** 15 seconds (longer for cold starts)
- **Has routes Railway doesn't:** `/api/agents/registry`, `/api/agents/intelligence-index`

**⚠️ WARNING:** Do NOT republish Replit casually. Cold starts can take 10-15 seconds. Discovery/auto-discovery cycles can consume all resources on startup and crash the server.

### 5. Neon PostgreSQL (SHARED DATABASE)

- **Host:** `ep-old-waterfall-aa2rwjzs-pooler.westus3.azure.neon.tech`
- **Database:** `neondb`
- **Role:** Single source of truth for all data — both Railway and Replit read/write to the same Neon instance
- **Accessed by:**
  - Cloudflare Worker (direct HTTP SQL queries for read-only routes)
  - Railway (via psycopg2/SQLAlchemy connection)
  - Replit (via psycopg2/SQLAlchemy connection)

**Key tables:**
- `facilities` — 20,000+ data center records
- `deals` — M&A transactions
- `news_articles` — Industry news
- `capacity_pipeline` — Pipeline projects
- `ai_cumulative` — AI platform tracking (cumulative counts)
- `ai_usage_tracking` — Per-request AI tracking
- `ai_platforms` — Platform metadata
- `platform_cards` — UI card configs for /ai page
- `ai_testimonials` — Agent testimonials
- `ecosystem_companies` — Industry ecosystem
- `users` — User accounts and subscriptions

---

## Failover Logic

The `mcp-proxy` Worker implements automatic failover:

```
Request arrives
    │
    ▼
Try Neon Direct (for read-only GET routes)
    │ fail?
    ▼
Try Railway (12s timeout)
    │ 502-504? → mark failure, try next
    │ 404 on /api/* path? → try next (Railway may not have the route)
    │ non-JSON 400+? → mark failure, try next
    ▼
Try Replit (15s timeout)
    │ fail?
    ▼
Serve stale cache (up to 2 hours old)
    │ no cache?
    ▼
Return 503 error
```

**Circuit breaker:** After 3 consecutive Railway failures, skip Railway entirely for 60 seconds before retrying.

---

## What NOT to Do (Lessons Learned)

### ❌ NEVER proxy root `/` to Railway
Railway returns API health JSON, not your website. The frontend lives in the `dchub` Worker.

### ❌ NEVER use `return fetch(request)` for non-API routes
There is no Cloudflare Pages origin. This causes a 522 timeout.

### ❌ NEVER deploy Worker changes without testing the homepage
Always verify `dchub.cloud/` returns HTML after any Worker deployment.

### ❌ NEVER assume Railway has all routes that Replit has
Some routes (agent registry, intelligence-index) only exist on Replit. The Worker's 404-fallthrough logic handles this.

### ❌ NEVER use `path` as a variable name in `proxyWithFailover()`
The function parameter is `pathname`. Using `path` causes silent undefined failures.

### ❌ NEVER disable the 404 fallthrough for /api/ routes
```javascript
// This line MUST exist in proxyWithFailover():
if (backendResponse.status === 404 && pathname.startsWith('/api/')) {
  continue;
}
```

### ❌ NEVER let Replit Agent or AI tools modify the Worker without human review
The Feb 26 outage was caused by an automated change that misunderstood the architecture.

---

## Deployment Checklist

### Before ANY Worker Change:
- [ ] Read this document
- [ ] Identify which routing paths are affected
- [ ] Check the Provider Map table above
- [ ] Have the previous Worker version saved as backup

### After Worker Deployment:
- [ ] `dchub.cloud/` → shows HTML homepage (NOT JSON)
- [ ] `dchub.cloud/ai` → shows AI Platform page
- [ ] `dchub.cloud/api/health` → returns JSON with facility_count
- [ ] `dchub.cloud/api/v1/failover-status` → shows primary/failover health
- [ ] `dchub.cloud/.well-known/mcp.json` → returns MCP server card
- [ ] `dchub.cloud/api/agents/registry` → returns agent list (not 404)
- [ ] Browser console has no new 404/502 errors

### Before ANY Railway Change (GitHub push):
- [ ] Changes are in `main` branch
- [ ] Railway auto-deploys — monitor logs for errors
- [ ] Verify: `curl https://dchub-backend-production.up.railway.app/health`
- [ ] Test affected endpoints through `dchub.cloud` (not direct Railway URL)

### Before ANY Replit Change:
- [ ] Do you REALLY need to change Replit? Railway is primary.
- [ ] Discovery/auto-discovery is disabled on Replit (IS_RAILWAY check)
- [ ] Do NOT republish unless absolutely necessary
- [ ] After restart, wait 60s for cold start before testing

---

## Version History

| Version | Date | Change | Impact |
|---|---|---|---|
| Worker v3.8.5 | Feb 26, 2026 | Fixed frontend routing to dchub Worker; fixed pathname bug in 404 fallthrough | Restored homepage after 15hr outage |
| Worker v3.8.4 | Feb 26, 2026 | **BROKE SITE** — proxied frontend to Railway instead of dchub Worker | 15hr outage showing raw JSON |
| Worker v3.8.3 | Feb 26, 2026 | Inline .well-known files served from Worker | Eliminated backend dependency for discovery |
| Worker v3.8.1 | Feb 25, 2026 | .well-known path rewriting for Railway | Fixed AI plugin discovery |
| Worker v3.8 | Feb 25, 2026 | Added testimonials Neon-direct routes | New feature |
| Worker v3.7 | Feb 24, 2026 | AI discovery file routing | Fixed 403/404 on discovery files |
| Worker v3.6 | Feb 23, 2026 | /dashboard proxied to Railway | New feature |
| Worker v3.5 | Feb 22, 2026 | AI tracking Neon-direct route | Fixed cumulative counts |
| Worker v3.4 | Feb 21, 2026 | Railway → primary, Replit → failover | Architecture swap |
| Worker v3.3 | Feb 20, 2026 | Fixed swapped backend URLs | Critical bug fix |

---

## Emergency Recovery

**If the homepage is showing JSON instead of HTML:**
The Worker is sending root requests to Railway. Fix line ~636 to route non-API paths to `https://dchub.azmartone.workers.dev`.

**If the homepage shows 522 timeout:**
Someone set non-API routes to `return fetch(request)`. There is no Pages origin. Route to `https://dchub.azmartone.workers.dev` instead.

**If API routes all return 503:**
Both Railway and Replit are down, and Neon direct queries are also failing. Check Railway logs first, then Replit, then Neon dashboard.

**If specific API routes return 404:**
Railway may not have that route. Verify the 404-fallthrough logic exists in `proxyWithFailover()`. If it does, check if Replit is up.

**Quick rollback:** Keep the previous Worker version saved. In Cloudflare Quick Edit, paste the old version and Save and Deploy. Takes 10 seconds.
