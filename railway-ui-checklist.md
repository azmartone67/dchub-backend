# Railway UI changes needed (#60, #61, #66)

After patching `extractor_cron.py` and pushing the change, set these env vars in the Railway dashboard.

## #60 — Daily refresh cron

**Service:** `desirable-playfulness` (the one running `extractor_cron.py` every 5 min)

**Add these env vars:**
| Variable | Value |
|---|---|
| `DAILY_SVC_URL` | `https://dchub-backend-production-f7dd.up.railway.app` |
| `REFRESH_SECRET` | (copy from `heroic-reprieve` → daily service → Variables → eye icon on `REFRESH_SECRET`) |

**How:**
1. https://railway.com → DC Hub project → `desirable-playfulness` service
2. Click **Variables** tab
3. **+ New Variable** → paste name + value (twice, once per row)
4. Save → service auto-redeploys (~1-2 min)

**Verify:** after first cron run post-deploy (within 5 min), check logs for `daily-refresh: HTTP 200`. Then refresh https://dchub.cloud/daily — date picker should start showing different snapshots day over day.

## #61 — R2 credentials on heroic-reprieve daily service

**Service:** `heroic-reprieve` daily service (the FastAPI app at `dchub-backend-production-f7dd.up.railway.app`)

**Add these env vars (or verify existing values):**
| Variable | Value |
|---|---|
| `R2_ACCOUNT_ID` | `4bb33ec40ef02f9f4b41dc97668d5a52` |
| `R2_ACCESS_KEY_ID` | (Cloudflare → R2 → Manage R2 API tokens → reuse existing or create new with read+write to `dchub-daily`) |
| `R2_SECRET_ACCESS_KEY` | (paired with above) |
| `R2_BUCKET` | `dchub-daily` |

**Verify:** trigger `/refresh`, check that `services/daily/storage.py` upload to R2 succeeds. Bucket should start filling with date-stamped PNGs.

## #66 — create-api-key dual-storage

This is a code change in dchub-backend, not a Railway env var. Two paths:

**Quick path (manual SQL, takes minutes):**
1. Get DB connection string from heroic-reprieve daily service Variables (`DATABASE_URL` or `DCHUB_DATABASE_URL`)
2. `psql $DATABASE_URL`
3. `INSERT INTO api_keys (key, plan, email, created_at) VALUES ('dchub_<value>', 'developer', 'vector-pipeline@dchub.cloud', NOW());`
4. Repeat for any keys minted via the worker that need to work against `/api/v1/facilities` etc.

**Proper path (PR in dchub-backend):**
- Add `/api/admin/sync-keys-from-cf-kv` endpoint that pulls from CF KV via worker proxy and writes to Postgres
- Have worker's `/api/admin/create-api-key` ALSO call this endpoint
- Document the sync as part of mint flow

For tonight, use Quick path. File the proper path as follow-up.

## #69 — Surface `/api/v1/search/semantic` in frontend

Three places:
1. `dashboard.html#api-keys` — add an example request snippet for the new endpoint
2. `pricing.html` — add "Semantic Search" as a Developer-tier feature bullet
3. MCP tool description in worker.js (already lives there as `search_facilities`; consider adding a separate `semantic_search` tool that calls Vectorize)

This is a separate dchub-frontend repo PR. Not in this Replit workspace.
