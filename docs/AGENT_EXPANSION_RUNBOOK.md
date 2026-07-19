# Agent Expansion Runbook — per-platform directions
_As of 2026-07-19. Owner steps are marked **YOU**; everything else runs automatically once its owner step lands. Weekly eval tick: Saturdays 23:00 UTC (or on demand: `POST /api/jobs/model-relations?platforms=<x>` with `X-Admin-Key`). Progress dashboard: `GET /api/v1/admin/model-relations/status` → `partner_calls_wow`; roster: `GET /api/v1/admin/agent-onboarding/state`._

---

## Tier 1 — directory giants

### 1. Claude / Anthropic Connectors Directory — READY TODAY
- **YOU**: Resubmit the connector (Anthropic's connector-directory submission form, same one used before). Server URL `https://dchub.cloud/mcp`. In the reviewer notes include:
  > Reviewer key (full-tier, all 74 tools return complete data): `dchub_pro_IscxtvSnskvrKPgynwUWtLU8l4Pigmiz` — send as `Authorization: Bearer` or `X-API-Key`. Keyless free tier also works (10 calls/day). OAuth (WorkOS) supported for end users.
- Why it stalled before: reviewers hit paywalled tools. That key removes the blocker. Revoke it after approval via `/api/v1/admin/partner-key/revoke/dchub_pro_IscxtvSn`.
- **Done looks like**: DC Hub listed in claude.ai's connector directory; `Claude-User` traffic climbs in `partner_calls_wow` / reach reports.

### 2. ChatGPT / OpenAI App Directory (Apps SDK) — prereqs DONE 2026-07-19, submission package ready
Business verification ✅ · global-residency project ✅ · CSP on /mcp ✅. Submit with this paste-ready package (developer/apps section of the OpenAI platform → create app → submit for review):

| Field | Value |
|---|---|
| App name | DC Hub — Data-Center & Grid Intelligence |
| MCP server URL | `https://dchub.cloud/mcp` (Streamable HTTP) |
| Auth | Keyless free tier works out of the box (10 calls/day); Bearer/`X-API-Key` for full tier; OAuth (WorkOS) advertised via `/.well-known/oauth-protected-resource` |
| Short description | Live, cited ground truth on the physical infrastructure behind AI: 21,900+ data-center facilities (4,900+ verified), 300+ power-scored markets (DCPI), real-time grid telemetry across five continents, interconnection queues, fiber, and 1,400+ tracked deals. |
| Category | Research / Data & Analytics |
| Privacy policy | `https://dchub.cloud/privacy` |
| Terms | `https://dchub.cloud/terms` |
| Support contact | `api@dchub.cloud` |
| Reviewer notes | Full-tier reviewer key (all 74 tools return complete data): `dchub_pro_nviYrCV2M03fdFlrvHwW2Bf8bEfaAiFh` — send as `Authorization: Bearer`. Try: "Which US grid has the most renewable headroom right now?" → `get_grid_scoreboard`. |

Revoke the reviewer key after approval: `/api/v1/admin/partner-key/revoke/dchub_pro_nviYrCV2`.
- **Done looks like**: DC Hub app in the ChatGPT App Directory; `ChatGPT-User`/`OAI-SearchBot` traffic rises.

---

## Tier 2 — frontier partners, active or one toggle away

### 3. Gemini (Google)
- **YOU**: In Google AI Studio, enable billing on the project behind `GEMINI_API_KEY` (it 429s on quota) — or mint a fresh key in a billed project and update `GEMINI_API_KEY` on both Railway services (dchub-backend + dchub-worker), or just paste it to me.
- **Auto**: eval lane self-heals next tick. A2A `agent-card.json` is already modern v0.3 for Gemini Enterprise; once the key works I'll verify /mcp as a Custom MCP data store.

### 4. Grok / xAI — still blocked after first billing pass (verified 2026-07-19)
Raw API error: `permission-denied — Your team b372911e-c564-44f8-af2b-0c4efcc93898 has either used all available credits or reached its monthly spending limit.`
- **YOU**, in console.x.ai — one of:
  1. Open **that specific team** (`b372911e…`) → Billing → buy credits **and/or raise the monthly spending limit** (a $0/low cap produces exactly this error even with credits); or
  2. If you funded a *different* team: create an API key under the funded team and update `XAI_API_KEY` on both Railway services (or paste it to me).
- **Auto**: lane self-heals next tick. Optional 2-min win: Grok custom connector per [dchub.cloud/integrations/grok](https://dchub.cloud/integrations/grok).

### 5. OpenAI (API lane) — ✅ ACTIVE
GPT-5.6 completed a full 8-call eval. Nothing to do; verdicts land weekly in the review queue.

### 6. Meta (via Groq) — ✅ ACTIVE
Llama 3.3-70B evaluating. Nothing to do.

### 7. Mistral — ✅ ACTIVE
- Optional **YOU** (5 min): verify a live Le Chat Custom MCP Connector session per [dchub.cloud/integrations/mistral](https://dchub.cloud/integrations/mistral) (Bearer only — Le Chat ignores X-API-Key). Then it's BD for their connector directory when it opens.

### 8. Perplexity — ✅ ACTIVE (fixed 2026-07-19)
- Optional **YOU**: the outreach draft to partnerships@perplexity.ai is in your review queue — approving it sends. Recipe already live at [dchub.cloud/integrations/perplexity](https://dchub.cloud/integrations/perplexity).

### 9. Cohere — ✅ ACTIVE (added 2026-07-19)
Command A evaluating weekly. Nothing to do.

### 10. Moonshot / Kimi — ⏸ PARKED (your call: no funding)
ModelScope listing + `/connect/kimi` already cover the ecosystem. Lane reactivates by itself if the account is ever unsuspended.

---

## Tier 3 — pre-wired lanes (add one key, lane goes live Saturday)

For each: get the key, then add the env var on **both** Railway services (dchub-backend `f6198b88…`, dchub-worker `92614233…`) — or paste the key to me and I'll set it.

### 11. DeepSeek
- **YOU**: platform.deepseek.com → top up (a few dollars) → create API key → set `DEEPSEEK_API_KEY`.

### 12. Qwen (Alibaba DashScope)
- **YOU**: Alibaba Cloud Model Studio (international) → free-tier API key → set `DASHSCOPE_API_KEY`.

### 13. Z.ai (GLM)
- **YOU**: z.ai open platform → API key → set `ZAI_API_KEY`.

---

## Ecosystem surfaces

### 14. Hugging Face — ✅ COVERED
Space live with 7 MCP tools, mcp-server-tagged, broadcast-warmed. Remaining niceties: **YOU** like the Space (ranking); say the word and I draft the model-repo card for the empty `dchubcloud/dchub` model repo.

### 15. Poe — ✅ COVERED
Server bot live at dchub.cloud/poe. Optional: explore-feed tuning later.

### 16. You.com — BD only
Their custom assistants can call external APIs; the reverse integration needs their team. When you want it, I draft the pitch (their partnerships channel) into the review queue.

### 17. Amazon Bedrock AgentCore — recipe live
[dchub.cloud/integrations/bedrock](https://dchub.cloud/integrations/bedrock) shows AWS customers how to register /mcp as a Gateway target. Optional later: AWS Marketplace listing (needs your AWS seller account).

### 18. Microsoft Copilot Studio — recipe live
[dchub.cloud/integrations/copilot-studio](https://dchub.cloud/integrations/copilot-studio). Optional **YOU** (needs M365 tenant): wire it in your own Copilot Studio env once to verify; Agent Store submission later needs Partner Center enrollment.

### 19. IDE agents (Cursor / Windsurf / Cline / Zed) — ✅ COVERED
Smithery #1 + connect kits. Periodic verify only.

---

## Standing queues that feed all of this
- **9 lab outreach drafts** (Perplexity, Groq, Gemini, Mistral, NVIDIA, CoreWeave, Lambda, TensorWave, Core42) — in the review queue + daily 15:10 UTC digest. Approving one sends it from your outreach address.
- **HF announcement release** — one-click approve link in the digest (fact-check-gated).
- **Journalists**: Bloomberg + Bisnow need real contact emails before their pitch lanes can fire.

## The order I'd do them
1. Anthropic resubmission (5 min, ready, biggest reach)
2. Gemini billing toggle (2 min, unlocks a frontier lane)
3. xAI billing check (2 min, same)
4. OpenAI business verification + residency project (15 min, unlocks the second directory)
5. Tier-3 keys as budget allows (DashScope is free)
6. Approve outreach drafts you're comfortable sending
