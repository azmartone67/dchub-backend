# DC Hub MCP — v2.1 Upgrade

Restores per-tool-call telemetry, adds X-API-Key validation/forwarding, gates paid-only tools, emits upgrade nudges, and fixes the discovery-file tool-list mismatch.

## Stack

- **Neon Postgres** — system of record (api_keys, mcp_call_log, etc.)
- **Railway (Node)** — `dchub-mcp-server` runs `server.mjs` on `dchub-backend-production.up.railway.app`
- **Railway (Python/Flask)** — backend API (the same Railway project that serves /api/v1/*)
- **Cloudflare Workers** — `dchubapiproxy` (front door, v3.8.7), `mcp-proxy`, `dchub-cron` (5-min refresh), `dchub-selfheal`, `dchub` (frontend)
- **Replit** — failover only; primary moved to Railway in dchubapiproxy v3.4 (Feb 21 2026)

Bottlenecks fixed by this release:
1. `server.mjs` had no per-tool telemetry (since Feb 5).
2. `server.mjs` ignored `X-API-Key`; backend couldn't tell who was calling.
3. `dchubapiproxy` `INLINE_DISCOVERY['/.well-known/mcp.json']` advertised 7 tools, only 2 of which existed on the server.

## Files

| File | Where it goes | What it does |
|---|---|---|
| `server.mjs` | dchub-mcp-server repo (Railway) | Patched MCP server with telemetry, key validation, tier gates |
| `flask_mcp_endpoints.py` | Flask backend (Railway) | Adds POST /api/v1/keys/validate, POST /api/v1/mcp/track, GET /api/v1/mcp/stats |
| `migration_001_api_keys.sql` | Neon (run once) | Creates `api_keys` and `mcp_call_log` tables |
| `gen_dev_key.py` | Local CLI | Mint, list, revoke, upgrade dev keys against Neon |
| `cf_worker_mcpjson_patch.js` | dchubapiproxy Worker | Replaces the broken 7-tool discovery list with the real 20 |
| `test_smoke.sh` | Local CLI | End-to-end smoke test once everything is deployed |

## Deploy order

### 1. Neon — run the migration

```bash
psql "$NEON_DATABASE_URL" -f migration_001_api_keys.sql
```

Or paste the file contents into the Neon SQL Editor and run. Verify:

```bash
psql "$NEON_DATABASE_URL" -c "\dt api_keys mcp_call_log"
```

### 2. Mint a test paid key (locally)

```bash
export NEON_DATABASE_URL='postgres://…@…neon.tech/…'
pip install 'psycopg[binary]>=3.2'
python gen_dev_key.py mint --email you@dchub.cloud --tier paid --note "smoke test"
# Copy the api_key it prints — you need it for the smoke test in step 6.
```

### 3. Flask backend (Railway) — register the blueprint

Drop `flask_mcp_endpoints.py` next to your other Flask modules.

In your `app.py` (or wherever the Flask app is built):

```python
from flask_mcp_endpoints import mcp_bp
app.register_blueprint(mcp_bp)
```

Add to `requirements.txt`:

```
psycopg[binary,pool]>=3.2
```

Railway env vars to confirm:
- `NEON_DATABASE_URL` — the Neon pooler URL (postgres://…neon.tech/…?sslmode=require)
- `DCHUB_INTERNAL_KEY` — must match Railway server.mjs and the Cloudflare Workers

Push, Railway redeploys. Smoke-check:

```bash
curl -X POST https://dchub-backend-production.up.railway.app/api/v1/keys/validate \
  -H "X-Internal-Key: $DCHUB_INTERNAL_KEY" \
  -H "Content-Type: application/json" \
  -d '{"api_key":"nope"}'
# Expected: {"valid": false, "tier": "free"}
```

### 4. Node MCP server (Railway) — replace server.mjs

In the `dchub-mcp-server` repo:

```bash
cp /path/to/this/dir/server.mjs ./server.mjs
git add server.mjs
git commit -m "MCP v2.1: telemetry, key validation, tier gates, platform detection"
git push origin main
```

Railway env vars on `dchub-mcp-server` to confirm:
- `DCHUB_API_BASE` = `https://dchub-backend-production.up.railway.app` (or the Cloudflare-fronted URL)
- `DCHUB_INTERNAL_KEY` — same as Flask
- `DCHUB_UPGRADE_URL` (optional, defaults to `https://dchub.cloud/ai#pricing`)
- `DCHUB_SIGNUP_URL` (optional, defaults to `https://dchub.cloud/ai`)

### 5. Cloudflare — fix the discovery file

Open the `dchubapiproxy` Worker in the Cloudflare dashboard → Edit Code. In the `INLINE_DISCOVERY` object near the top, replace the `'/.well-known/mcp.json'` entry with the block in `cf_worker_mcpjson_patch.js`. Save and deploy. Bump the `worker:` version comment at the top.

Verify:

```bash
curl -s https://dchub.cloud/.well-known/mcp.json | jq '.tools | length'
# Expected: 20
```

### 6. Smoke test end-to-end

```bash
chmod +x test_smoke.sh
MCP_URL=https://dchub.cloud/mcp \
API_KEY=dch_live_<your-paid-test-key> \
DCHUB_INTERNAL_KEY=$DCHUB_INTERNAL_KEY \
./test_smoke.sh
```

You should see 6 green checks plus a "telemetry round-trip ok" line with non-zero `tool_calls`.

### 7. Confirm tracking is live

```bash
curl -H "X-Internal-Key: $DCHUB_INTERNAL_KEY" \
  https://dchub-backend-production.up.railway.app/api/v1/mcp/stats?days=1 | jq
```

You should see:
- `funnel.tool_calls` ≥ 5 (from the smoke test)
- `funnel.keyed_devs` ≥ 1
- `by_tool` listing `search_facilities`, `analyze_site`, etc.

That's the moment the verification gap is closed and we can start measuring real upgrade behavior.

## Tier rules (in server.mjs)

Free tier:
- Capped to 25 results on `search_facilities`, `get_pipeline`, `get_infrastructure`
- Capped to 10 on `list_transactions`
- Capped to 20 on `get_news`
- **Blocked** from `analyze_site`, `compare_sites`, `get_grid_intelligence`, `get_dchub_recommendation`, `get_fiber_intel` — these return `paid_only` with `upgrade_url`

Paid / enterprise: no caps, all tools available.

Edit the `FREE_TIER_LIMITS` and `PAID_ONLY_TOOLS` constants in `server.mjs` to retune.

## Optional: Cloudflare Hyperdrive for Neon

You don't have any Hyperdrive configs today. If the Workers start hitting Neon's connection limit during traffic spikes, set up Hyperdrive: Cloudflare → Workers & Pages → Hyperdrive → Create config, point at the Neon pooler URL, then bind it to `dchubapiproxy` and replace `env.NEON_DATABASE_URL` with `env.HYPERDRIVE.connectionString` in the Worker. Not required for v2.1 to ship — just on the backlog.

## Rollback

The Node server is the highest-risk change. Keep the old `server.mjs` in git. If something breaks:

```bash
git revert <commit-sha>
git push origin main
```

Railway redeploys to the previous version automatically. The Flask blueprint is additive — leaving it in place after a Node rollback is safe; it just won't be called.

The Cloudflare discovery patch is also additive — the only risk is some agent that was hard-coded against the wrong tool names breaks. Almost nobody is doing that, but the rollback is just to revert the Worker.

The Neon migration is purely additive (new tables, no destructive DDL). No rollback needed.
