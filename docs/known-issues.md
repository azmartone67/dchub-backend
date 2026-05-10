# Known issues (Railway-side, not GHA-fixable)

## Smoke test failures

### `search` → HTTP 429
The smoke test hits `/api/v1/search` without an API key. Free tier rate-limits
free-IP requests to ~10/day, but the smoke test is on a CI runner whose IP
shares with many free users — so it consistently hits the rate limit.

**Fix:** set `DCHUB_SMOKE_API_KEY` repo secret with a Pro-tier key. Update
`tools/fixkit/dchub_qa_v2.py` to send `X-API-Key: $DCHUB_SMOKE_API_KEY`
on the search test.

### `watchdog` → HTTP 503
The `/api/energy-discovery/status` endpoint returns 503 when the ingestion
worker pool is starved or restarting.

**Fix:** Railway-side scheduler. Investigate `crawler_scheduler.py`
registration in main.py and Railway's worker process manifest.

## Ingestion staleness (real signal)

The watchdog correctly reports `recent_syncs: []` and zero counts on
pipelines/wind/gas/transmission while HIFLD substations + EIA power plants
populate fine — i.e. dynamic-fetch ingestion is dead while seeded data is OK.

Phase 165 made the watchdog NOT fail the GHA workflow when this is detected;
the alert lives in workflow log output (grep ISSUE-FALLBACK).
