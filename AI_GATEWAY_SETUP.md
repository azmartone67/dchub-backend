# Cloudflare AI Gateway — DC Hub setup runbook

**Goal:** route 100% of DC Hub's Claude traffic through Cloudflare AI Gateway
for **spend limits, caching, and per-workload cost visibility** — the brain
runs Opus 4.8 (1M context) across ~12 layers every 5 min, plus press +
market-brief + report narratives. That is the bulk of the AI bill.

## What's already done (code) ✅
- **SDK clients** (`utils/anthropic_helper.get_anthropic_client`) honor
  `ANTHROPIC_BASE_URL` — all 11 SDK call sites route through the gateway with
  zero code change.
- **Raw-HTTP call sites** (20+: brain L4/L5/L7/L8/L9/L14/L16/L18/L23,
  marketing, market-brief, report narratives, news extraction, citation
  trackers) now call `anthropic_helper.anthropic_messages_url()` instead of a
  hardcoded `api.anthropic.com` — they route through the gateway too.
- The brain-reasoning call is tagged with `cf-aig-metadata` for per-component
  attribution; `aig_metadata_headers(component, cache_ttl=…)` is available for
  any call site that wants its own tag/caching.
- **Everything is env-gated** — behavior is identical to today until you set
  the env var. Zero risk to deploy.

## Turn it on (owner — Cloudflare dashboard + Railway, ~5 min)

### 1. Create the gateway
Cloudflare dashboard → **AI** → **AI Gateway** → **Create Gateway**.
Name it e.g. `dchub`. Note your **Account ID** (in the URL / right rail).
The Anthropic provider endpoint is:
```
https://gateway.ai.cloudflare.com/v1/<ACCOUNT_ID>/dchub/anthropic
```

### 2. Point DC Hub at it (Railway → Variables)
Set on **every service that calls Claude**:
- `resourceful-essence` (main Flask backend) — **required**
- `dchub-backend-render` (Render HA mirror) — same code, set it there too
- `heroic-reprieve` (DC Hub Daily) — if it generates Claude content

```
ANTHROPIC_BASE_URL = https://gateway.ai.cloudflare.com/v1/<ACCOUNT_ID>/dchub/anthropic
```
(`ANTHROPIC_API_KEY` stays as-is — the gateway forwards it to Anthropic.)

Redeploy (or it picks up on the next deploy). Verify in the boot log:
```
[ai-gateway] ✅ ACTIVE — routing Anthropic via gateway.ai.cloudflare.com
```
or hit `anthropic_helper.get_status()`.

### 3. Set spend limits (the new feature)
AI Gateway → your gateway → **Settings → Spend limits** (open beta, all plans).
Start in **monitoring mode** with a high cap for ~a week to learn the baseline,
then enforce. Suggested first budgets (dollars, monthly):
- A global cap as a hard ceiling.
- Per-component once attribution data accrues: filter the analytics by the
  `component` metadata tag (`brain-reasoning`, `press`, `market-brief`, …).
When a limit is hit you can **block** or **fall back to a cheaper model** via
Dynamic Routes (so the brain degrades instead of dying).

### 4. Enable caching (free money)
AI Gateway → **Settings → Cache** → turn on. The brain re-issues identical
radar/critique/consistency prompts constantly → exact-match cache hits cost $0.
(Per-call TTLs are also supported via `aig_metadata_headers(..., cache_ttl=N)`.)

### 5. (Optional) Identity-driven budgets — closed beta
Per-user / per-team budgets via Cloudflare Access. Sign up for the closed beta
if you want per-employee attribution; not needed for the core cost control.

## Verify it's working
1. Boot log shows `[ai-gateway] ✅ ACTIVE`.
2. AI Gateway dashboard → **Logs/Analytics** shows requests flowing, with the
   `component` metadata tags.
3. `get_status()` returns `{"active": true, ...}`.

## Rollback (instant)
Unset `ANTHROPIC_BASE_URL` (and `DCHUB_AI_GATEWAY_URL`) → all calls go straight
to `api.anthropic.com` again. No deploy needed beyond the env change.

## Next (Cloudflare roadmap)
CF announced **task-based intelligent routing** (auto-route each request to the
cheapest model that meets quality) — in active development. When it ships, no
code change needed here; configure it in the gateway. Until then, use Dynamic
Routes for fallback-on-spend-limit.
