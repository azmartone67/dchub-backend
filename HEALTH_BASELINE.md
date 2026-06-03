# DC Hub — Health Baseline (known-good state)

**As of 2026-06-03.** This file memorializes the configuration and numbers that
define a healthy DC Hub, and points at the automated fences that keep them from
silently drifting. When something feels "off," diff reality against this doc.

This is the snapshot after a long stabilization + honesty pass: the platform went
from inflated-and-fragile to honest-and-stable across 3 repos and 5 registries.

---

## 1. Green baseline (what "healthy" looks like)

| Signal | Healthy value (2026-06-03) |
|---|---|
| Site Sentinel | **68/68 pages healthy (100%)** — `/api/v1/sentinel/scan` |
| `/pricing` | **HTTP 200 in ~0.15s** (was a 20s hang — see §6) |
| Most pages | <100ms static · ~0.8–1.3s live-DB dynamic (markets/grid) — normal |
| MCP funnel | ~33k tool calls / 7d, growing — `/api/v1/mcp/funnel` |
| Smithery | ~1,276 RPC / 440 sessions / 30d · ~1.2% error |
| Backend | 2 Railway replicas, Neon PG, single pool 5–50 conns |

## 2. Verified canonical numbers — THE honest-numbers truth

Checked live at the Railway origin. These are the ONLY values to publish. The
fence in §5 fails the build if a forbidden one returns.

| Metric | Real value | Say | NEVER say |
|---|---|---|---|
| M&A deals | `COUNT(*)`=**2,032** | "2,000+ tracked deals" | **"$324B"** (uncomputable; `value_usd` sparse; live route falls back to $85B) |
| Countries | **178** | "170+" | "140+" |
| DCPI markets | **233** (`/api/v1/dcpi/scores`) | "232" | **280+/285/286/289/276** (SPP-clone inflation, deduped in fix #43) |
| MCP tools | **31** (manifest incl. Worker-only `semantic_search`) | "31" | (server.mjs `tools/list` = 30; both defensible) |
| Active MCP clients | **Claude + Cursor** | "used by Claude and Cursor" | "96+/90+ AI platforms"; long "cited by ChatGPT, Gemini, Perplexity, Groq" lists |

## 3. Config invariants (settings that MUST hold)

**Frontend (`~/dchub-frontend/`):**
- `_routes.json`: `/pricing` is in **`exclude`** (served static natively) — NOT `include`. `/pricing/*` stays in `include` (checkout → Flask). *Putting `/pricing` back in `include` re-creates the 20s hang.*
- `_redirects`: `/pricing.html → /pricing` (301) — the canonicalization that, combined with the worker, caused the hang. Leave the route in `exclude`.
- `_worker.js`: the `/pricing` handler is hardened (2.5s `Promise.race` cap + guaranteed return) as defense-in-depth.

**Backend (`~/dchub-backend/`):**
- `routes/surface_brain.py`: `_SURFACES_TTL_S >= 300` (currently 600) — the cold recompute is ~5s; a low TTL stalls a gunicorn worker.
- `routes/facility_profile_page.py`: facility pages keep `Cache-Control: max-age=3600` — Googlebot crawls ~12.8k enriched pages; cache them at the edge, not the origin.
- `routes/site_qa.py`: a `code==0` failure with `response_ms >= 10000` is a HANDLER timeout, **not** DNS (the bug that hid `/pricing`).

## 4. Architecture map (so you fix the right surface)

**3 deploy pipelines:** `dchub-backend` → Railway (Flask) · `dchub-frontend` → Cloudflare Pages (static + `_worker.js`) · `dchub-mcp-server` → Railway (Node `server.mjs`).

**4 tool-description surfaces** (a numbers change must hit all relevant ones):
1. `server.mjs` `trackedTool(...)` → `tools/list` for connected agents.
2. Flask `.py` → `/.well-known/mcp.json` + ~45 marketing/SEO copies.
3. CF Pages `_worker.js` + `ai.json`/`skill.json`/`server-card.json`.
4. **Out-of-repo `dchubapiproxy` Cloudflare Worker** → serves the LIVE `dchub.cloud/.well-known/mcp.json` (wins over Flask). **CF-dashboard-only** — local `worker.js` is stale. See §7 #1.

## 5. The fences (what protects this baseline)

- **`tests/test_honest_numbers.py`** — runs in `pre-merge.yml` (`pytest tests/`). Greps the live backend source and FAILS the build if `$324B`, an inflated platform count, or an inflated market count creeps back, and asserts the §3 cache floors. (Already caught a stray `289 Markets` headline on day one.)
- **`tests/test_tier_consistency.py`** — locks the tier/price canonical map.
- **Brain outcome-verifier** (`routes/site_sentinel.py:verify_outcomes` + `surveillance-sweep.yml`, every 15 min) — re-probes open findings, marks **resolved** (logs recovery + downtime to `site_sentinel_resolutions`) or **stuck** (down >2h, escalated). The brain now learns which fixes worked vs which are stuck.
- **`scripts/regression_lint.py --mode delta`** — the pre-merge delta lint.

## 6. Runbook — if X regresses

- **`/pricing` hangs again** → check `_routes.json`: `/pricing` must be in `exclude`. If it drifted into `include`, move it back. (Root cause: worker `env.ASSETS.fetch()` on an in-`include` path falls through to the backend proxy.)
- **Sentinel flags a page stuck >2h** → it'll surface as a `::warning` in the surveillance-sweep run; `/api/v1/sentinel/resolutions` shows recovery history.
- **`$324B` / inflated count reappears** → the pre-merge guard blocks it; the failure message names the file:line and the correct value.
- **Site-wide flapping** → almost always a slow synchronous endpoint exhausting the single-pool workers; cache it (see §3) — don't add replicas.

## 7. Human-only follow-ups (can't be automated)

1. **CF `dchubapiproxy` worker** — one `$324B` line remains in the out-of-repo `mcp.json`. CF dashboard → Workers → `dchubapiproxy` → Edit Code → replace `Tracks $324B+ in deals.` → `2,000+ tracked deals (disclosed value where public).` → Save & Deploy.
2. **Google Search Console** — re-submit `sitemap.xml` to nudge re-crawl of the ~12.8k newly-enriched facility pages.
3. **Journalist outreach** — drafts auto-stage at `/admin/partnerships/review`; add the editor email you know + approve to send (we never guess journalist emails or auto-send).
