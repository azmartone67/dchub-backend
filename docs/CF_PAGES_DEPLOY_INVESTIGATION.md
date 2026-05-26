# Cloudflare Pages Deploy Investigation — r57 (2026-05-25)

## TL;DR

**The CF Pages auto-deploy is working** since `r46.5` (commit
`46410676300`, 2026-05-25 16:35 UTC) fixed the `_routes.json`
overlap rejection. The user-visible "_worker.js still won't
auto-deploy" complaint had TWO underlying causes — both
documented + fixed in this round:

1. **False-positive "Worker version drift" warning** in the
   `Verify worker version after deploy` step of
   `.github/workflows/deploy-pages.yml`. The grep matched on TWO
   lines (the actual `x-dc-worker-version:` header AND the
   `access-control-expose-headers:` value, which lists header
   *names* including `X-DC-Worker-Version`). Multi-line `$deployed`
   never equalled single-line `$expected` → warning even when the
   deploy was a clean success.
   **Fix**: anchor grep with `^x-dc-worker-version:` and add `head -1`.

2. **Stale CF edge cache** masquerading as failed deploy. The
   `/api/v1/dcpi/scores?limit=1` response has `cache-control:
   max-age=3600`, so an edge cache populated with the *old*
   worker's `x-dc-worker-version` header gets served unchanged
   for up to 1 hour after a deploy. Cache-buster on the verify
   curl confirms the fresh value.

## Evidence

Workers in the account (via `mcp__cloudflare__workers_list`):

| name          | modified_on            | role                                         |
|---------------|------------------------|----------------------------------------------|
| dchubapiproxy | 2026-05-25 21:15 UTC   | this repo's Pages worker (`_worker.js`)      |
| mcp-proxy     | 2026-05-24 06:50 UTC   | out-of-repo `4.8.5-mcp-landing` zone worker  |
| dchub-cron    | 2026-05-02 17:39 UTC   | scheduled cron tasks                         |
| dchub-selfheal| 2026-05-05 00:09 UTC   | recovery utility                             |

`dchubapiproxy` modified TODAY → Wrangler upload from
`deploy-pages.yml` IS landing.

Recent `deploy-pages.yml` runs:

| time (UTC)         | status     | commit                                    |
|--------------------|------------|-------------------------------------------|
| 2026-05-26 02:14   | in_progress| partners: route /partnerships/dashboard… |
| 2026-05-26 01:54   | success    | r56-frontend: llms.txt                    |
| 2026-05-26 01:48   | success    | feat(cited-by): Groq citation card        |
| 2026-05-26 01:08   | success    | r53-frontend: align facility count claims |
| 2026-05-25 16:40   | success    | Deploy Cloudflare Pages (manual)          |
| **2026-05-25 16:35** | **success** | **r46.5 CRITICAL: fix _routes.json overlap** |
| 2026-05-25 16:24   | failure    | r46.4 frontend                            |
| 2026-05-25 15:51   | failure    | r45 (worker version bump)                 |

Before r46.5, **every** auto-deploy failed because Wrangler 3.40
strict-rejected the `_routes.json` overlap. After r46.5, every
auto-deploy succeeded — but the verify step kept emitting
warnings (cause #1 above) so the user reasonably concluded
"still won't deploy."

## Live probe (post-fix)

With cache-buster:
```
$ curl -sI "https://dchub.cloud/api/v1/dcpi/scores?limit=1&_t=99887766" \
    | grep -iE 'x-dc-worker-version|cf-cache'
cf-cache-status: MISS
x-dc-worker-version: 4.34.15-r45-pages-force-redeploy
```

Without cache-buster:
```
$ curl -sI "https://dchub.cloud/api/v1/dcpi/scores?limit=1" \
    | grep -iE 'x-dc-worker-version|cf-cache'
cf-cache-status: REVALIDATED
x-dc-worker-version: 4.24.0-switzerland       # ← stale cache!
```

This explains why earlier rounds appeared to see a stuck worker:
**they were reading from a stale CF edge cache.** The
`REVALIDATED` status only confirms that CF asked origin "is this
still good?" — origin says yes (because `cache-control` allows
the 1h window), so CF serves the original cached body+headers,
including the OLD worker version.

## Fixes shipped in r57

1. `.github/workflows/deploy-pages.yml` — anchor the grep, add
   cache-buster to the verify curl, document the cache caveat
   inline.

2. (Optional follow-on, NOT shipped yet) Lower `max-age` for
   the verify endpoint, or add a dedicated `/api/v1/version`
   endpoint with `Cache-Control: no-store` so the verify step
   never has to fight the cache. This requires a worker-side
   change and is queued for r58.

## How to audit yourself

```bash
# 1. Local source version
grep -oE "WORKER_VERSION\s*=\s*'[^']+'" \
  /Users/jonathanmartone/dchub-frontend/_worker.js | head -1

# 2. Live worker version (cache-busted)
curl -sI "https://dchub.cloud/api/v1/dcpi/scores?limit=1&_cb=$RANDOM" \
  | tr -d '\r' | grep -i '^x-dc-worker-version:'

# 3. Last 5 GH Actions deploys
gh run list -R azmartone67/dchub-frontend \
  --workflow=deploy-pages.yml --limit 5

# 4. Last CF Worker modification time (via MCP)
#    mcp__cloudflare__workers_list → look for dchubapiproxy
```

If (1) and (2) match → deploy is current. If they differ AND
the cache-buster was applied → deploy genuinely didn't propagate
(rare; usually a transient CF edge issue resolved within minutes).

## What's still out of scope

- The out-of-repo `mcp-proxy` worker (`4.8.5-mcp-landing`) is
  deployed by hand or by a separate dashboard configuration.
  This investigation does NOT cover that deploy path. See
  `reference_dchub_prod_alias_pin.md` in the user memory.
- Pages Functions vs Workers as distinct entities — CF blurs
  this in the dashboard. For our purposes:
  `_worker.js` + `wrangler.toml` in `dchub-frontend/` →
  `dchubapiproxy` Worker via `wrangler pages deploy`.
