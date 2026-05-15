# dchub-neon-failover

Cloudflare Worker that provides Railway-independent failover for the 5
most critical DC Hub read endpoints. When Railway times out or 5xx's,
this Worker answers the same request by querying Neon directly through
Cloudflare Hyperdrive.

This is the "Railway → CF backup" pattern the user asked about during
the watchdog-restart-loop incident (May 2026): when Railway went into a
SIGTERM cycle and most of `/api/v1/*` was unreachable, the public site
went dark. With this Worker live, `/health`, `/stats`, `/freshness/radar`,
`/dcpi/scores`, and `/facilities/state-status-counts` keep responding —
the homepage, the audit dashboard, the radar, and `/daily` all stay up.

## Endpoints covered

| Route | What it returns |
|-------|-----------------|
| `/api/v1/health` | `{status, db}` heartbeat |
| `/api/v1/stats` | Facility/deal/substation/fiber/gas/pipeline counts |
| `/api/v1/freshness/radar` | All 11 data domains with status/age/SLA |
| `/api/v1/dcpi/scores` | Latest DCPI scores per market |
| `/api/v1/dcpi/scores/<slug>` | Single market full DCPI snapshot |
| `/api/v1/facilities/state-status-counts` | Per-US-state op/uc/ann counts |

## How it behaves

1. **Try Railway first** (12s timeout — env-overridable via
   `FAILOVER_TIMEOUT_MS`). If Railway responds 2xx, the Worker proxies
   it as-is and sets `X-DCHub-Source: railway`.
2. **On Railway failure** (timeout / 5xx / network error), compute the
   same response from Neon directly. Set `X-DCHub-Source: neon-failover`.

You can read the `X-DCHub-Source` response header to know which leg
served any given request. The body shape is identical either way (modulo
the `note`/`source` field that explicitly says "neon-failover" so
client-side dashboards can flag failover events).

## Deploy

Prereqs: a Neon connection string in `NEON_DATABASE_URL`.

```bash
cd cf-workers/neon-failover
npm install -g wrangler          # if you don't have it
wrangler login                    # one-time browser auth

# 1. Create a Hyperdrive config pointing at Neon
wrangler hyperdrive create dchub-neon \
  --connection-string "$NEON_DATABASE_URL"
# -> copy the returned `id` into wrangler.toml's hyperdrive.id

# 2. Deploy
npm install                       # pulls in `pg`
wrangler deploy
```

Then in the Cloudflare dashboard:

1. Workers & Pages → `dchub-neon-failover` → **Triggers**
2. Add 5 routes (all on the `dchub.cloud` zone):
   - `dchub.cloud/api/v1/health`
   - `dchub.cloud/api/v1/stats`
   - `dchub.cloud/api/v1/freshness/radar`
   - `dchub.cloud/api/v1/dcpi/scores`
   - `dchub.cloud/api/v1/dcpi/scores/*`
   - `dchub.cloud/api/v1/facilities/state-status-counts`

The Worker becomes the primary handler for those paths and tries Railway
first; if Railway succeeds, the response is identical to before. If it
fails, the Worker takes over from Neon. The existing `_worker.js`
Pages-Functions worker continues to handle every other route.

## Reading the failover signal

You can monitor failover events by polling any of the 5 routes and
inspecting the `X-DCHub-Source` header:

```bash
curl -sI https://dchub.cloud/api/v1/health | grep -i x-dchub-source
```

- `X-DCHub-Source: railway` → Railway healthy.
- `X-DCHub-Source: neon-failover` → Railway down, Worker covered.
- `X-DCHub-Source: neon-failover-error` → both legs failed.

A reasonable health alert: notify when `neon-failover` shows up for >5
consecutive minutes on `/api/v1/health`.

## Why these 5 endpoints

They're the public read paths that power the most-visited surfaces:
- `/health` — every other probe depends on it
- `/stats` — homepage hero tiles, every dashboard
- `/freshness/radar` — the audit page (`/audit`) + my self-awareness ask
- `/dcpi/scores` — `/dcpi`, `/markets/*`, the DCPI press kit, the OG cards
- `/facilities/state-status-counts` — the `/daily` infographic

All five are pure DB reads — no writes, no auth, no third-party calls.
Perfect for Hyperdrive's connection-pooled edge proxy.

## What's NOT covered (by design)

- POST endpoints (`/keys/identify`, `/alerts/subscribe`, etc.) — writes
  go through Railway only; the funnel can wait for recovery.
- Per-user / authenticated paths — Workers don't share Railway's session
  state.
- MCP tool calls — the MCP Node server is its own Railway service; its
  failover would be a separate Worker (out of scope for this kit).

## Cost / limits

- Cloudflare Workers free tier: 100k requests/day.
- Cloudflare Hyperdrive: included with Workers Paid ($5/mo), no per-query
  charge.
- Neon: connection-pooled by Hyperdrive, so the Worker's per-request
  query doesn't open a fresh Postgres connection each time.

Expected steady-state usage when Railway is healthy: ~0 Neon queries
(Railway answers, Worker proxies). Failover events: dozens to thousands
of Neon queries per minute depending on traffic — Hyperdrive handles it.
