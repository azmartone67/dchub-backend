# CF Worker Heartbeat Snippet — r47.39

The `dchub-selfheal` Cloudflare Worker runs 294 times / day (per your CF
analytics) but the source-registry says "never ran" because the worker
doesn't call the /heartbeat endpoint. Paste this at the top of each
scheduled handler and the worker starts reporting in.

## For dchub-selfheal

In the worker's `scheduled()` handler, add at the very top:

```javascript
// r47.39: source-registry heartbeat — proves the worker is alive
await fetch('https://api.dchub.cloud/api/v1/sources/cf-selfheal/heartbeat', {
  method:  'POST',
  headers: {
    'Authorization': `Bearer ${env.DCHUB_ADMIN_SECRET || 'dchub-admin-secret-2026'}`,
    'Content-Type':  'application/json',
  },
  body: JSON.stringify({
    status:        'success',
    rows_affected: 1,
    metadata:      { trigger: 'cf_scheduled', worker: 'dchub-selfheal' },
  }),
  signal: AbortSignal.timeout(5000),
}).catch(() => {});  // silent — never block the worker's main work
```

## For dchub-cron

Same snippet, change the source ID:

```javascript
await fetch('https://api.dchub.cloud/api/v1/sources/cf-dchub-cron/heartbeat', {
  // ... same as above, swap source_id
});
```

## For arcgis-proxy

```javascript
await fetch('https://api.dchub.cloud/api/v1/sources/cf-arcgis-proxy/heartbeat', {
  // ... same as above, swap source_id
});
```

## Deploy

If you use Wrangler locally:
```bash
cd <worker-repo>
wrangler deploy
```

Or paste directly in CF Dashboard → Workers & Pages → <worker> → Edit Code,
then Deploy.

## Verify

After one scheduled run fires (~5 min for selfheal cron), check:
```bash
curl -s "https://dchub.cloud/api/v1/sources/cf-selfheal" | python3 -m json.tool
# Should show last_success_at within last 5 min, enabled=true
```
