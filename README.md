# DC Hub Backend — `dchub-backend`

> **⚠️ Read [DEPLOYMENT_LOCK_v3.md](DEPLOYMENT_LOCK_v3.md) before making ANY infrastructure changes.**

## Architecture

```
dchub.cloud (Cloudflare DNS)
    │
    ├── mcp-proxy Worker (dchub.cloud/*) ─── Smart Router
    │   ├── Non-API paths → dchub Frontend Worker (HTML/CSS/JS)
    │   ├── Read-only GETs → Neon PostgreSQL (direct SQL)
    │   ├── API writes/auth → Railway (this repo) → Neon
    │   └── Failover → Replit → Neon
    │
    ├── dchub Frontend Worker ─── Static site (HTML/CSS/JS)
    │   └── Source: cloudflare/worker-frontend-dchub.js
    │
    └── Neon PostgreSQL ─── Shared database (single source of truth)
```

## Repository Structure

```
dchub-backend/
├── main.py                          # Flask API backend (Railway + Replit)
├── agent_network_effect.py          # Agent registry + intelligence index routes
├── requirements.txt                 # Python dependencies
├── Procfile                         # Railway: gunicorn entrypoint
├── railway.json                     # Railway build config
├── nixpacks.toml                    # Railway nixpacks config
│
├── cloudflare/                      # Cloudflare Worker source code (version controlled)
│   ├── worker-mcp-proxy-v3.8.5.js  # API proxy/router Worker (dchub.cloud/*)
│   └── worker-frontend-dchub.js    # Frontend Worker (static HTML site)
│
├── DEPLOYMENT_LOCK_v3.md            # ⚠️ CRITICAL: Architecture map & deployment rules
└── README.md                        # This file
```

## Provider Roles

| Provider | Role | URL |
|---|---|---|
| **Cloudflare `mcp-proxy`** | API router + failover | `dchub.cloud/*` |
| **Cloudflare `dchub`** | Frontend (HTML/CSS/JS) | `dchub.azmartone.workers.dev` |
| **Railway** | Primary API backend | `dchub-backend-production.up.railway.app` |
| **Replit** | Failover API backend | `dc-hub-replit-fixedzip--azmartone1.replit.app` |
| **Neon PostgreSQL** | Shared database | `ep-old-waterfall-aa2rwjzs-pooler.westus3.azure.neon.tech` |

## Deploying

### Railway (auto-deploys from `main` branch)
Push to `main` → Railway rebuilds automatically. Monitor logs for errors.

### Cloudflare Workers
1. Open Cloudflare Dashboard → Workers & Pages
2. Select the worker (`mcp-proxy` or `dchub`)
3. Quick Edit → paste updated code from `cloudflare/` folder
4. Save and Deploy
5. **Always verify homepage loads HTML after any Worker change**

### Replit
Replit runs the same `main.py` but with background tasks disabled. Do NOT republish unless necessary.

## Key Rules

1. **Non-API routes → `dchub` frontend Worker** (never Railway, never `fetch(request)`)
2. **Railway root `/` returns JSON** — it is NOT the homepage
3. **API 404s on Railway fall through to Replit** (404 fallthrough logic in Worker)
4. **Both backends share one Neon database** — no data sync needed
5. **Read DEPLOYMENT_LOCK_v3.md before changing anything**

## Environment Variables (Railway + Replit)

Both backends need identical env vars:
- `DATABASE_URL` / `NEON_DATABASE_URL` — Neon connection string
- `STRIPE_SECRET_KEY` — Payment processing
- `STRIPE_WEBHOOK_SECRET` — Stripe webhook verification
- `ADMIN_API_KEY` — Admin endpoint auth
- `IS_RAILWAY` — Set only on Railway (controls background task execution)
