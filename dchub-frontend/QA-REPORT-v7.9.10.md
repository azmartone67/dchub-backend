# DC Hub v7.9.10 — QA Report

**Date:** 14 April 2026
**Scope:** Six production bug families reported by Jonathan plus a new
pre-deploy QA harness to keep them squashed.

## Executive summary

Every bug reported for this pass is a **frontend-only regression** —
the Railway/Python backend is reachable through the Cloudflare Worker
proxy and serves valid JSON. What was breaking was:

1. static files calling the wrong URL,
2. a `_redirects` rule hiding a static directory,
3. a nav href pointing to the wrong page, and
4. a chart that collapsed all undated rows onto today's date.

No backend code had to change. All 6 bugs are fixed in v7.9.10 and are
now detected statically by `qa/squasher.py` before a deploy can ship
them.

---

## Bugs fixed

### BUG-033 · Assets Explorer shows 0 facilities (CORS)

**Evidence:** Console shows
`Access-Control-Allow-Credentials header ... is '' which must be 'true'`
against both `web-production-e6382.up.railway.app` and
`dchub-backend-production.up.railway.app`.

**Root cause (two-part):**

1. `assets.html` listed `web-production-e6382.up.railway.app` as a
   fallback backend. That Railway service was **decommissioned in
   March 2026**. Every request to it fails at the network layer.
2. The second fallback hits Railway directly with
   `credentials: 'include'`, so Chrome requires
   `Access-Control-Allow-Credentials: true` on the preflight. The
   Railway CORS config doesn't set it, so the preflight fails and the
   request is blocked.

**Fix (`assets.html`):** Route through the CF Worker only
(`API_BACKENDS = ['']`). The Worker's `addCORS` helper (line 656 of
`_worker.js`) already sets `Allow-Credentials: true`. Same-origin means
no preflight at all.

```diff
- const API_BACKENDS = [
-     '',
-     'https://web-production-e6382.up.railway.app',
-     'https://dchub-backend-production.up.railway.app'
- ];
+ const API_BACKENDS = [''];  // same-origin only — CF Worker proxies
```

Also removed the JWT auth-patch's cross-origin branch so credentials
are only ever attached on same-origin requests.

**Also patched:** `map.html` had the same dead-URL issue.

---

### BUG-034 · Capacity Pipeline chart collapses to "Apr 26"

**Evidence:** Screenshot shows a single bar at `Apr 26` with ~100 GW
stacked. Every other month is empty.

**Root cause:** In `capacity-pipeline.html` line 1182:

```js
const date = new Date(item.announcement_date || item.created_at || Date.now());
```

When the backend returns rows without `announcement_date` or
`created_at` (common for pipeline records sourced from scraped press
releases), `Date.now()` is used → everything lands in April 2026.

**Fix:** Added a `pickDate(item)` helper that scans 10 plausible date
fields (`announcement_date`, `announced_date`, `expected_online`,
`expected_completion`, `online_date`, `commissioning_date`,
`rfc_date`, `target_date`, `updated_at`, `created_at`). Rows with no
usable date are **excluded from the timeline** (with a console.log
count) rather than dumped on today.

Also widened the visible window from 12 to 18 months.

---

### BUG-035 · AI Integrations nav link sends traffic to `/ai`

**Root cause:** `js/dchub-nav.js` line 84 had:

```js
{ label: 'AI Integration', href: '/ai', ... }
```

So the Platform dropdown's "AI Integration" tab loaded `/ai.html`
(the AI landing page) instead of `/ai-integrations.html` (the MCP
status dashboard the user was trying to get to).

**Fix:** Split into two explicit items:

```js
{ label: 'AI Integrations', href: '/ai-integrations', ... },
{ label: 'AI Hub',          href: '/ai',              ... },
```

The squasher's R3 rule now enforces this mapping — any future drift
in href is blocked at build time.

---

### BUG-036 · Press page shows "Unable to load press releases"

**Root cause:** `press.html` lines 685-686 tried Railway **first**,
same-origin second:

```js
var API_URL = 'https://dchub-backend-production.up.railway.app/api/press-releases';
var FALLBACK_URL = '/api/press-releases';
```

Same CORS trap as BUG-033 — Railway direct fails preflight, fallback
is only invoked on network error (not a CORS block, which surfaces as
`ok=false` and still calls `onload`). The page renders the "Unable to
load" message before ever trying same-origin.

**Fix:** Swapped order — same-origin first, Railway direct fallback.

---

### BUG-037 · `/markets` section disappeared

**Root cause:** `_redirects` line 39 had:

```
/markets                       /market-intelligence     301
```

Cloudflare Pages evaluates `_redirects` before static files, so
`/markets/` (which has an `index.html` plus 59 city pages) never got
served — every request bounced to `/market-intelligence`.

**Fix:** Deleted the rule. The static directory serves again. Added a
new `/market-intel → /market-intelligence 301` so the short alias
still works.

**Added:** Also put "Markets" back in the Intelligence nav dropdown as
a distinct item from "Market Analytics" (which keeps the old route).

---

### BUG-038 · Testimonials console noise (503s)

**Evidence:** Console repeatedly logs
`[DC Hub] Session sync failed: auth/me failed: 503`. The page itself
renders fine via fallback data — this is purely noise + a failed
session-sync.

**Root cause:** Railway's `/api/auth/me` intermittently 503s under
load. `js/dchub-access-gate.js` treated every non-200 as a hard
failure and logged a warning.

**Fix:** Treat 5xx/429/0 as **transient**:
- swallow them silently on the current pageload,
- schedule a retry after 60 s,
- keep the cached session intact.
Only real 401/403 clear the session now.

---

### Front-page markets banner (new feature)

Added a horizontally-scrolling ticker of 39 featured markets to
`index.html`, positioned between the hero metrics and the feature
grid. Each pill links to `/markets/{slug}` and shows facility count,
MW capacity, and Tier 1/2/3 class.

The ticker pauses on hover, loops seamlessly via a `-50%` translate,
and degrades gracefully on reduced-motion. Styling borrows from the
GDCI page's data-chip aesthetic as requested.

The slug list in the banner is verified at build time by squasher rule
**R7** — if a slug is renamed or deleted without updating index.html,
the deploy is blocked.

---

## New: QA harness (`qa/`)

| File           | Purpose                                                  |
| -------------- | -------------------------------------------------------- |
| `squasher.py`  | Pre-deploy static source scan. 7 rules. Stdlib only.     |
| `smoke.py`     | Post-deploy HTTP smoke tests. 9 tests. Needs `requests`. |
| `predeploy.sh` | Wrapper: squasher + `node --check` on each JS file.      |
| `README.md`    | Coverage matrix + wiring examples.                       |

Both scripts exit `0`/`1` for CI, and support `--json` for structured
output. They're stdlib-only so they run anywhere — including Replit.

### Wiring it in

Add to Cloudflare Pages build command:

```bash
python3 qa/squasher.py && npm run build
```

Add as a GitHub Action after a `wrangler deploy`:

```yaml
- run: |
    pip install requests
    python3 qa/smoke.py --base https://dchub.cloud --fail-fast
```

---

## Verification run

Squasher on the fixed tree:

```
$ python3 qa/squasher.py .
✓ Squasher: clean. No findings.
```

Smoke tests are ready but need to run **after** the v7.9.10 deploy
ships (they hit the live site). Expected result: 9/9 pass. If any
test fails in production, re-open the corresponding BUG-### in
`admin-qa.html` — both dashboards reference the same numbering.

---

## Backend notes (informational — no change made)

The Replit/Railway backend is serving correctly for the tested routes,
but the screenshots show two transient issues that the frontend now
tolerates:

- `/.well-known/mcp/server-card.json` → 500 Internal Server Error
  (seen on AI Integrations page). Worth investigating the Python
  handler; `smoke.py S9` will alert when it regresses.
- `/api/v1/mcp/platforms` + `/api/v1/mcp/analytics` → 503 (same page).
  If these are intermittent, they'll pass the smoke test most of the
  time; set the cron to alert on 3 consecutive failures rather than 1.

Both are now covered by smoke tests (S9 and, indirectly, S7) so you'll
know the moment they come back.
