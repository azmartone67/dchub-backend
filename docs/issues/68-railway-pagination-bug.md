# Railway pagination/filter bug — Issue #68

**Status:** Open — investigation needed
**Filed:** 2026-04-27
**Severity:** Medium (data correctness on paginated endpoints)
**Affected service:** dchub-backend (Railway, gunicorn main:app)
**Related:** #66 (closed — dual-storage api_keys), iteration 4 (shipped)

## Summary
Paginated list endpoints on the Flask API return inconsistent results when
combining `limit` / `offset` with structured filters (`country`, `provider`,
`status`, `min_capacity_mw`, etc.). Users report duplicates across pages,
missing rows, or filters appearing to be ignored.

## Suspect endpoints
Highest-traffic paginated routes — start here:

- `GET /api/v1/facilities` — list facilities (limit/offset/country/provider/min_capacity_mw)
- `GET /api/v1/deals` — list M&A transactions (limit/offset/buyer/seller/region)
- `GET /api/v1/pipeline` — list pipeline projects (limit/offset/status/min_capacity_mw)
- `GET /api/news` — list news articles (limit/offset/category)

## Reproduction (template — fill in actuals when reproducing)

```
# Page 1
curl -sS -H "X-API-Key: $DCHUB_API_KEY" \
  "https://dchub-backend-production.up.railway.app/api/v1/facilities?limit=10&offset=0&country=US" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print([(x.get('id'), x.get('name')) for x in d.get('data', d.get('facilities', d.get('results', [])))])"

# Page 2 — should be different rows
curl -sS -H "X-API-Key: $DCHUB_API_KEY" \
  "https://dchub-backend-production.up.railway.app/api/v1/facilities?limit=10&offset=10&country=US" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print([(x.get('id'), x.get('name')) for x in d.get('data', d.get('facilities', d.get('results', [])))])"
```

If page 1 and page 2 share rows, or page 2 returns the same rows as page 1,
the bug is reproduced.

## Most likely root causes (ranked)

1. **Missing `ORDER BY` clause** — without a deterministic sort, Postgres is
   free to return rows in any order on each call. The same `OFFSET 10 LIMIT 10`
   may yield different rows from one query to the next.
   **Fix:** add `ORDER BY id` (or any unique-not-null column) before `LIMIT/OFFSET`.

2. **Filter parameters not bound into the WHERE clause** — list handlers
   often build SQL with f-strings that drop the filter when the param is empty
   or the wrong type (e.g. `min_capacity_mw='30'` as string vs `30` as int).
   **Fix:** parameter-bind via `cur.execute(sql, (params,))` consistently;
   coerce types at the boundary; log the final SQL for QA.

3. **`OFFSET` clamped or ignored** — some routes have a `MAX_OFFSET` guard or
   silently set `offset = 0` when the param is non-numeric, leaving the user
   stuck on page 1.
   **Fix:** validate `offset` as a non-negative integer; return 400 on
   bad input rather than silently rewriting.

4. **Cache key collision** — if a Cloudflare KV / proxy layer caches by URL
   without including all query params in the key, page 1's response gets
   served for page 2.
   **Fix:** ensure cache keys include the full querystring (or skip caching
   for paginated reads).

## Triage commands

```
# Find every list handler in main.py — focus on ones that read OFFSET/LIMIT
grep -nE "@app\.route.*api/v1/.*list|OFFSET|LIMIT.*%s|LIMIT.*\?" /home/runner/workspace/main.py | head -30

# Find list handlers WITHOUT an ORDER BY — strongest candidates for cause #1
grep -BnA 6 "OFFSET.*LIMIT" /home/runner/workspace/main.py | grep -B6 LIMIT | grep -v ORDER

# Confirm cache layer — does dchubapiproxy cache list endpoints?
grep -n "cache" /home/runner/workspace/worker.js | grep -iE "facilities|deals|pipeline" | head
```

## Workaround (until fixed)
- Use cursor-style pagination (`?after_id=NNN`) on endpoints that support it
- Or fetch with very large `limit` and paginate client-side
- Filter values must match exact case + type that the backend expects

## Fix proposal
1. Add a `_paginate(query, params, limit, offset)` helper in `main.py` that
   appends `ORDER BY id LIMIT %s OFFSET %s` and validates `limit ≤ 100`,
   `offset ≥ 0`. Route every list handler through it.
2. Add a `pagination` block to the response envelope:
   `{"limit": 50, "offset": 100, "total": 21319, "has_more": true}`.
   Frontend can stop paging when `has_more=false`.
3. Add an integration test that asserts `page_1 ∩ page_2 == ∅` for the four
   most-trafficked list endpoints.

## Tracking
- Discovered during: iteration 4 hybrid retrieval testing
- Code paths to inspect: list handlers in `main.py` (estimated 8–12 sites)
- Estimated fix size: ~50 lines plus tests
