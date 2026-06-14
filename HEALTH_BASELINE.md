# DC Hub — Health Baseline (known-good state)

**As of 2026-06-13 (r85).** This file memorializes the configuration and numbers that
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
| DCPI markets | **300** (Neon dedup 2026-06-08: COUNT(DISTINCT market_name)−3 aggregates; raw 306) | "300" / "300+" | **340+** (gross over-claim; ~300 is real, grew from 232 via intl) |
| MCP intelligence tools | **38** (marketed + fenced count). `tools/list` returns **39** incl `claim_free_key` — a r85 **utility** tool (the anon→identified conversion lever), intentionally NOT in the marketed "38 intelligence tools". `GET /mcp` status shows 39 (raw). | "38" (intelligence) | 11/19/20/24/30/31/33/40 (stale drift — fenced front + back) |
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
- **Brain model tiers** (`routes/brain_models.py`, env-driven): `DCHUB_BRAIN_MODEL_INSPECTOR` + `_REASONING` = **`claude-opus-4-8`** — **NOT `claude-fable-5`** (Fable went `http_404` on this account ~2026-06-13: *"Claude Fable 5 is not available. Please use Opus 4.8"*; `claude-haiku-3-5` also 404). Opus 4.8 is the best reachable model (more capable than Fable + 1M-context). **Verify with `GET /api/v1/brain/model-probe` (`best_reachable`) before pinning ANY tier.** **AUTO-ENABLE (r85j):** `DCHUB_BRAIN_PREFER_FABLE=1` is SET — `brain_model_for` is reachability-aware and will auto-promote inspector/reasoning back to `claude-fable-5` the moment a probe confirms it reachable (it uses fable ONLY on positive confirmation; runs on opus-4-8 while it's 404). The cron `brain-model-reachability.yml` (every 2h) hits model-probe → persists the map to brain_meta `brain_model_reachability`. No manual flip either direction. The `/api/v1/brain/ask` fallback now fires on ANY model 404 (the old gate required the word "model" in the error body; Fable's said "not available" → it silently failed and the whole answer 404'd).

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

1. **CF `dchubapiproxy` worker** — one `$324B` line remains in the out-of-repo `mcp.json`. CF dashboard → Workers → `dchubapiproxy` → Edit Code → replace `Tracks $324B+ in deals.` → `2,000+ tracked deals (disclosed value where public).` → Save & Deploy. **Also bump its `version` 2.1.20 → 2.2.4** while in there.
2. **Google Search Console** — add the GSC service-account email as an owner/full user on the `sc-domain:dchub.cloud` property (`/api/gsc/status` shows `verified:false` — that's the blocker; the env var + OAuth already work), THEN re-submit `sitemap.xml` (UI or `POST /api/v1/gsc/sitemap/submit`). Do **not** wire the Indexing API for facility pages (Google restricts it to JobPosting/BroadcastEvent — policy risk).
3. **Journalist outreach** — drafts auto-stage at `/admin/partnerships/review`; add the editor email you know + approve to send (we never guess journalist emails or auto-send).
4. **MCP registry claims** (raise the media-organism `source_of_truth`/distribution scores): claim Glama (owner-only), fill `mcphive.com/submit.html`, nudge `stacklok/toolhive-catalog#1252` + `lobehub/lobehub#15667`, claim yellowmcp.
5. **`api.dchub.cloud` non-`/api/*` routing** — returns 522 (dead origin) for `/grid/<ISO>` etc. CF-dashboard-managed; either fix the origin or retire the subdomain. r78 already pointed internal probes off it (warmer → `dchub.cloud`, heartbeats → loopback).

## 8. r78 invariants (2026-06-12 platform-wide QA pass)

The big stabilization pass. New invariants that MUST hold — diff reality against these when self-healing/funnel/traffic regress:

**Brain self-heal loop (was dead 06-05 → 06-12):**
- `routes/brain_v2_layer4.py:_call_claude` — `max_tokens=4000` (NOT 800: reasoning-tier models exhaust 800 on thinking → `no_text_block`/`parse_fail`), checks `stop_reason=='max_tokens'`, joins ALL text blocks. *Layer-5 proposals flatlined for 12 days because fable-5/opus on an 800-token budget never emitted a text block.*
- `routes/brain_v2_layer5.py:learn_backend_issues` — permafail slow-lane (deterministic-failure outcomes retry ~weekly via crc32, not every cycle) + cycle-rotation offset + writes `brain_learning_log` (so `last_log_at` reflects real Layer-5 activity, not just the dead Layer-4 frontend path).
- `routes/internal_bot_circuit_breaker.py` — **loopback (`127.0.0.1`/`::1`) is exempt.** This breaker (30 req/min/UA) was 429-ing the brain's OWN localhost radar/heartbeat probes (the 06-12 429 flood). `remote_addr` is the socket peer — unspoofable. Never widen to UA/Referer (CF injects a dchub.cloud Referer).
- `routes/brain_autopilot.py:_compute_heartbeat_sync` returns a plain dict (NOT `jsonify` — it runs in a daemon thread with no app context; the old tail crashed every cold-start with "Working outside of application context").

**Replica safety (2 replicas — leader-election EXISTS at `main.py:4793`, advisory lock `911714323`):**
- Per-cycle `is_current_leader()` gate now wraps: Twitter/Bluesky publishers (`content_publisher.py`), crawler scheduler (`crawler_scheduler.py` — gates ~60 email/social jobs/day), self-heal (`dchub_self_heal.py`), deal ingester, package-stats, mcp-gateway health loop. *Before: all ran on BOTH replicas → double-posts/emails/work.*
- Publishers + brain now START on every non-failover replica (per-cycle gate idles followers) — a promoted follower must not go dark on leader churn (gunicorn `--max-requests` recycles drop the lock).
- Keepalive re-acquire error (`main.py:~4894`) keeps PRIOR leader state — never promotes a follower on a transient Neon blip (that made TWO leaders).
- Editor-rejected social posts are marked `status='rejected'` (terminal) + `_record_media_block` — a LIMIT-1 oldest-first queue wedges forever on a rejected head row otherwise (X shipped 0/7d behind post 750 for 5 days).

**Self-traffic diet (was ~150k self-requests/day through the public edge):**
- `dchub_self_heal.heal_cycle` — leader-gated + **DB-backed interval gate** (`self_heal_events` `__cycle__` marker, 5.5h). The APScheduler "6h interval" was fiction: `--max-requests` recycling re-ran the 60s `heal_warmup` ~every 35 min → 71k DCHubHealer req/day.
- `routes/brain_consistency_radar.py:check_dead_internal_links` — deadlink cache persists to `brain_meta` (survives worker recycles + spans replicas), not just in-process.
- `grid_cache_warmer.py` — `BASE=https://dchub.cloud` (was `api.dchub.cloud` → 522s); `HOT_ISOS` = the 9 REAL `/grid/<x>` routes only (dropped TVA/SOCO/FRCC/BPA/AESO — EIA BAs with no page → chronic 5xx ×48/day).
- `dchub_heartbeat.py` + `routes/cron_heartbeat.py` — on Railway, BASE = `127.0.0.1:$PORT` (internal telemetry to itself via loopback, not the edge).
- `/agents` → `301 /agent` (`routes/quick_redirects.py`) — was the #1 4xx path (4.65k/day, 404 at edge+origin).

**Funnel (activated→identified was 0% by construction):**
- `flask_mcp_endpoints.py:identify_key` falls through to `auto_trial_keys` for `dch_trial_*` keys (they live there, NOT `mcp_dev_keys` — identify failed for 100% of the trial cohort). Binding raises the cap 15→50/day.
- `routes/auto_trial.py:stats` counts EITHER `operator_email` OR `signed_up_email` as identified; `active_unique_callers_7d` counts keys USED (not mint-time IP-hash, which was crawler-dominated).
- `server.mjs:buildAutoMintBlock` leads the email CTA with the real 15→50/day incentive (was buried "Optional").

**Dashboards (truth):**
- `/api/v1/mcp/analytics` — external-only headline (excludes dchub-selfheal/probes which were 93% of calls), weighted per-call avg (not mean-of-means), ISO timestamps (not RFC-1123 → "Invalid Date"), windowed recent_calls.
- `ai-integrations.html` — `parseUTC()` handles all 3 backend timestamp shapes; 38-tool header; one-directional logo match (`t` no longer claims Anthropic's logo); prefers backend `logo_url`.

**Fences:** frontend `scripts/accuracy_fence.py` now bans stale tool counts (`11/19/20/24/30/31/33 Tools`, `of N available`, `"tools_count": <not 38>`) — wired in `deploy-pages.yml`. Backend `tests/test_honest_numbers.py` already bans prose counts. **38 tools is canonical across both.**

**MCP traffic-collapse note:** the "35.6k calls/7d" headline was ~93% internal `dchub-selfheal`. Real external MCP traffic fell 39.7k→~500 calls/wk (May→June); the May peak was ~the owner's own enterprise key (`dchub_live_08f4f…`, azmartone@gmail.com). Diagnosing the real-external collapse (candidates: scraper-block r-5/27, paywall reframe ~6/1, a Claude.ai connector change) is the top open growth question — a funnel fix can't convert traffic that's gone.

## 9. r85 invariants (2026-06-13 — brain reasoning restored + MCP conversion truth)

**★ Brain reasoning was 404ing — Fable 5 went dark.** See §3 for the pin. `DCHUB_BRAIN_MODEL_INSPECTOR` + `_REASONING` were both on the now-404 `claude-fable-5` → Inspector briefs + reasoning silently degraded for hours. Flipped to `claude-opus-4-8`. Verified: a forced `POST /api/v1/brain/brief/generate` now returns `ok:true, model:claude-opus-4-8`. When the user asks *"is brain still thinking as Fable 5?"* — the honest answer is **no, it's Opus 4.8 now (an upgrade)**, reversible when Fable access returns.

**MCP "funnel leak" is a DENOMINATOR ILLUSION — the funnel is HEALTHY.** Raw `tool_calls_7d`≈36k but `distinct_callers_7d` (COUNT DISTINCT `ip_address`, 7d) ≈ **66** — the same ~66 callers loop ~540×/wk. Honest free→paid on the real base ≈ **28%** (16 paid of 57 non-ent keys); on the flagship paid tool ≈ 9% of its 173 distinct free users. That's a HEALTHY SaaS rate, NOT the 0.02% the old briefs panicked over. **NEVER divide conversions by raw `tool_calls`.** `routes/brain_inspector.py` now carries `distinct_callers_7d` in the `mcp_funnel` signal + a prompt rule so briefs stop screaming "99% leak." **Schema trap:** `mcp_tool_calls` has **no `api_key` column** (cols: `tool_name/platform/client_name/ip_address/session_id/...`) — use `ip_address`. (r85e shipped `api_key`, the new Opus brief caught it as "funnel query broken at the schema level", r85f fixed it — verify columns via `information_schema`, never assume.)

**The ONE real conversion lever = anon→identified.** 99.7% of paywall hits are anonymous agents that loop + leave no identity to bill (conversions falling while upgrade-signals rise = intent leaking out with no identity attached). **`claim_free_key` MCP tool** (`server.mjs`, r85, **the 39th tool**) mints a free dev key in ONE call (no email/browser, via `/api/v1/keys/claim`) so an agent self-identifies the instant it hits a paywall. NOT in `PAID_ONLY_TOOLS` (anon-callable); `trackedTool` telemetry measures adoption; paywall responses point at it (`claim_free_key_tool` field + message line). **Proof metric to watch:** free-tier `keys_by_tier` rising in step with `upgrade_signals_7d` + `time_to_convert` compressing. Partly resolves the §8 "real-external collapse" question: the real base never went to zero (~66/wk loyal callers); the lever is converting MORE of them via identity, not chasing a phantom.

**`/api/v1/brain/ask`** (L9 conversational) now reasons with the Opus reasoning tier on SEGMENTED funnel data (was hardcoded `claude-sonnet-4-5` on raw counts) — ask it conversion/strategy questions directly; it returns `model_used`. **`GET /mcp` status strings** (`server.mjs` ~L2803: `version`/`tools`) are hardcoded SEPARATELY from the `McpServer` init (~L2023) — bump BOTH on a version change or the status endpoint lies.

**L22 WALK autonomy — 2 real-PR recipes (human-merge; RUN/auto-merge stays OFF).** `real_pr_whitelist = [route_alias_404, cron_if_mismatched]` (r85h). The cron recipe (`open_cron_stagger_pr` in `routes/brain_layer22_pr_writer.py`) staggers ONE colliding GitHub Actions cron. ★ INVARIANT — it must NEVER edit `evolve-cron.yml` or ANY workflow containing `github.event.schedule` (a job-guard trap: changing a cron string there silently disables a job); DR-canonical workflows (`backup-neon-r2`/`iso-data-pull`/`dchub-daily-status`) stay on their slot; plain-integer-minute crons only; yaml-validated; DRAFT PR; fork-only. If you touch `_apply_cron_stagger_to_workflows`, keep all of those fences (4/4 synthetic tests in this session proved them). Gated behind `DCHUB_L22_REAL_PR=1`.
