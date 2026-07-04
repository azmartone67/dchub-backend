# ADR: OAuth 2.1 durable identity for dchub.cloud/mcp (hosted-web agents)

- **Status:** PROPOSED (design only — no auth code changed by this ADR)
- **Date:** 2026-07-04
- **Track:** DF-4 (OAuth durable identity)
- **Decision drivers:** 1.2% key-reuse rate; hosted-web agents (Claude.ai, ChatGPT, Le Chat)
  cannot persist an `X-API-Key` header, so every session re-mints an anonymous trial identity
  and all usage history / retention / conversion attribution forks per session.
- **Related:** `reference_dchub_workos_oauth_chain`, `reference_dchub_onboarding_fragmentation`,
  PR #1416 (MCP_OAUTH_ADVERTISE gate), gateway commits `aaf39ca` / `e47b0ff` / `5ee704f`.

---

## 1. Decision (one architecture)

**WorkOS AuthKit is the OAuth 2.1 Authorization Server; dchub stays a pure Resource Server.
OAuth access tokens are mapped — at the existing gateway/backend seam — onto existing
`mcp_dev_keys` records, so tiers, limits, paywalls, and attribution keep working unchanged.
Existing `X-API-Key` auth is untouched forever; OAuth is additive behind the flags that
already exist.**

Concretely (all of this is ALREADY BUILT and was proven end-to-end on 2026-06-21 against the
WorkOS sandbox; what remains is productionizing, metadata coherence, and identity merge):

```
Claude.ai / ChatGPT / Le Chat
      │  401 + WWW-Authenticate: Bearer resource_metadata=…       (gateway server.mjs)
      ▼
GET /.well-known/oauth-protected-resource[/mcp]                    (CF worker dchub-oauth-meta)
      → { resource: https://dchub.cloud/mcp,
          authorization_servers: [https://auth.dchub.cloud] }      ← today: []  (Stage 1 fixes)
      ▼
AuthKit (AS): RFC 8414/OIDC discovery · CIMD + DCR · PKCE S256 · consent · tokens (+refresh)
      ▼
POST /mcp  Authorization: Bearer <workos JWT, aud=https://dchub.cloud/mcp>
      │  resolveWorkosBearer(): jose JWKS verify (sig+iss+exp) + aud enforce (RFC 8707)
      ▼
POST /api/v1/oauth/identity  (internal-key gated, backend)
      → get-or-create mcp_dev_keys row  api_key = dch_oauth_<HMAC(secret,"workos:"+sub)[:32]>
      ▼
everything downstream (validateKey, tier gating, trimForTrial, mcp_calls_identity,
billing, funnel) sees a normal api_key — zero changes needed
```

### Why AuthKit-as-AS beats the alternatives

| Option | Verdict | Reasoning |
|---|---|---|
| **A. AuthKit as AS + dchub as RS** (chosen) | ✅ | Purpose-built MCP support (WorkOS "Connect"): dashboard toggles for **DCR and CIMD**, Resource Indicators (stamps `aud` = our resource), consent UI, refresh-token rotation, JWKS — we validate JWTs statelessly and never own token security. The whole RS half (`resolveWorkosBearer`, `/api/v1/oauth/identity`, 401 challenge, aud enforcement, retry/no-negative-cache hardening) is already merged and live-tested. CIMD matters: the current MCP spec (2025-11-25; draft strengthens this) makes CIMD the SHOULD and deprecates DCR — Claude.ai and ChatGPT both prefer CIMD; self-rolling CIMD support is real work, AuthKit ships it as a checkbox. |
| B. Self-rolled AS (`oauth.mjs`, dormant) | ❌ as primary; keep dormant as DR fallback | Complete and well-hardened (PKCE S256-only, atomic single-use codes via `oauth_store` DELETE…RETURNING, caps/ratelimits, consent page), but: (1) consent-only anonymous identity — it mints a fresh free key per OAuth `client_id`, so there is no *human* identity, no verified email, weaker merge semantics; (2) DCR-only, no CIMD — against the ecosystem direction; (3) we own the entire AS attack surface (token storage, code replay, redirect validation) on a two-replica Flask/Express estate that already has enough moving parts; (4) Claude.ai DCR behavior registers a new client per connection → unbounded `oauth_store` client growth is *our* problem instead of WorkOS's. |
| C. Minimal token endpoint on the backend | ❌ | Strictly worse than B — same ownership burden without B's finished hardening. An MCP AS in 2026 must ship RFC 8414/OIDC discovery + CIMD or DCR + PKCE + consent + refresh rotation to pass Claude/ChatGPT validators; "minimal" is not a thing the validators accept. |

---

## 2. Current estate (audited read-only 2026-07-04)

### 2.1 Live surfaces (verified with curl on 2026-07-04)

| Surface | Live behavior | Served by |
|---|---|---|
| `GET /.well-known/oauth-protected-resource` and `…/mcp` | 200, `authorization_servers: []`, OIDC scopes, `mcp_protocol_version: 2025-06-18` | out-of-repo CF worker **`dchub-oauth-meta`** (zone route beats dchubapiproxy; edited live 07-03 via CF API PUT to empty the AS list). NB: the repo mirror `workers/dchub-oauth-meta.js` referenced in memory is **absent** from the repo — the live worker currently has no source-of-truth in git. |
| `GET /api/v1/oauth-protected-resource` (+`/mcp`) | 200, `authorization_servers: []` (`MCP_OAUTH_ADVERTISE` unset → gate closed; PR #1416) | Flask `routes/mcp_oauth_2025_06_18.py` |
| `GET /.well-known/oauth-authorization-server` | 404 | CF worker 404s; dormant self-AS off |
| `POST /mcp` (no key, `clientInfo.name=claude-ai`) | **401** + `WWW-Authenticate: Bearer resource_metadata="https://dchub.cloud/api/v1/oauth-protected-resource", scope="openid profile email offline_access"` | gateway `server.mjs` challenge block (method-aware since 5ee704f: only `initialize` + `tools/call`) |
| Frontend `_worker.js:2216` | third PRM block advertising `https://beloved-stream-52.authkit.app` — routing never selects it, but it is a third disagreeing manifest | `~/dchub-frontend/_worker.js` |

### 2.2 ⚠️ Live inconsistency (headline finding — Stage 0 fixes this)

**The 401 challenge is ACTIVE while every reachable PRM advertises `authorization_servers: []`.**
Two 07-03 workstreams collided: the flywheel fix wave made the challenge method-aware (i.e. it
is armed — `DCHUB_WORKOS_OAUTH_ENABLED=1`, `DCHUB_OAUTH_CHALLENGE_DISABLE` unset), while the
onboarding-QA wave emptied the PRM's AS list to stop forced WorkOS logins. Net effect today:
a keyless Claude.ai **web** connector gets 401 on `initialize`, fetches the PRM, finds no
authorization server (Claude uses the first entry, **no fallback**), and cannot complete
either OAuth *or* anonymous connect. Claude Code / keyed / sessioned / non-Claude callers are
unaffected (they are not challenged). This must be resolved to ONE coherent posture before
anything else in this ADR ships.

### 2.3 Auth code inventory (the pieces we build on — none of them change until the stages below)

- **Gateway** `~/dchub-mcp-server/server.mjs` (Express, Railway `dchub-mcp-server`):
  - X-API-Key chain (canonical, forever): `validateKey()` → backend `POST /api/v1/keys/validate`, 5-min cache; Bearer without other meaning is treated as an X-API-Key.
  - `resolveWorkosBearer()` (~L1133): flag `DCHUB_WORKOS_OAUTH_ENABLED` + `WORKOS_AUTHKIT_DOMAIN`; jose `jwtVerify` against `<domain>/oauth2/jwks` (sig+iss+exp); `aud` must include `DCHUB_MCP_RESOURCE` (default `https://dchub.cloud/mcp`, RFC 8707) — `DCHUB_WORKOS_AUD_ENFORCE` default ON (never disable: confused-deputy); email from JWT claim (preferred; AuthKit JWT template) or WorkOS Management API (`WORKOS_API_KEY`, backend-side too); maps to durable key via internal `POST /api/v1/oauth/identity` with 3× retry and **no negative-cache on transient failure** (e47b0ff — prevents the 401-storm/flap); 5-min positive / 1-min negative (invalid-JWT-only) token cache.
  - 401 challenge (~L6018): fires only for `clientInfo.name === "claude-ai"` or UA `Claude-User`, only on `initialize`/`tools/call`, only with no X-API-Key, no valid WorkOS bearer, no live session. Kill switch `DCHUB_OAUTH_CHALLENGE_DISABLE=1`.
  - Dormant self-AS `oauth.mjs` (`DCHUB_OAUTH_ENABLED`, off): full OAuth 2.1 AS + DCR + consent; durable via backend `oauth_store`; `dcht_` opaque tokens. Keep dormant (see §1 option B).
- **Backend** (Flask, Railway `dchub-backend`):
  - `routes/mcp_oauth_2025_06_18.py`: PRM (Flask paths incl. the non-well-known `/api/v1/oauth-protected-resource` the challenge points at — deliberate: the CF worker owns `/.well-known/*`); AS advert double-gated on `WORKOS_AUTHKIT_DOMAIN` + `MCP_OAUTH_ADVERTISE`; stale RFC 8414 doc + 501 DCR/token stubs (custom `read:*` scopes, issuer `api.dchub.cloud`) — to be retired in Stage 1; **`POST /api/v1/oauth/identity`** — internal-key gated; deterministic `dch_oauth_<HMAC(JWT_SECRET,"workos:"+sub)[:32]>` get-or-create in `mcp_dev_keys` (tier seeded `free`, `ON CONFLICT(api_key) DO NOTHING` resolves races), stamps `last_used_at` on every resolve (retention measurable), backfills email.
  - `routes/oauth_store.py`: durable store for the dormant self-AS. Inert.
- **Key systems** (unchanged by this ADR): `mcp_dev_keys` (`dch_trial_` auto-mint anon, `dch_live_` paid, `dch_oauth_` OAuth; keyed by email, no user_id) and `api_keys` (`dchub_` REST, keyed by user_id) — the two-namespace fragmentation is tracked in `reference_dchub_onboarding_fragmentation` (PRs #1416/#1417/#1419/#1422/#1424) and is orthogonal here except for merge (§4).
- **WorkOS estate:** AuthKit was proven with sandbox `safe-countryside-77-sandbox.authkit.app`
  (gateway env as of 06-21); `beloved-stream-52.authkit.app` appears in the frontend worker and
  the 07-03 QA notes. **The web app does NOT use WorkOS for human login** (`auth_routes.py` is
  `users.password_hash`) — AuthKit here is AS-for-MCP only, which is fine: AuthKit's
  "Standalone MCP Auth" mode exists precisely for keeping an existing login system.
  Which WorkOS environment is production is Open Question #1.
- One real durable key exists: `dch_oauth_b93a27bf…` (owner's test, manually upgraded).

---

## 3. What the spec and the platforms require (researched 2026-07-04, official sources)

### 3.1 MCP authorization spec — current revision **2025-11-25** (draft strengthens the same lines)

- RS **MUST** serve RFC 9728 PRM; clients **MUST** use it for AS discovery (Claude.ai probes
  the **path-suffixed** variant `/.well-known/oauth-protected-resource/mcp` for a server at
  `/mcp` — both variants must answer, which the CF worker already does).
- AS **MUST** offer RFC 8414 *or* OIDC Discovery; clients MUST support both. (AuthKit does.)
- **CIMD** (OAuth Client ID Metadata Documents) is now **SHOULD** for AS + clients; **DCR
  (RFC 7591) is deprecated** but retained for back-compat. Practical consequence: the AS
  should support both; AuthKit exposes each as a dashboard toggle.
- Clients **MUST** send RFC 8707 `resource` on authorize + token calls; RS **MUST** validate
  token audience. (Gateway already enforces `aud`; AuthKit Resource Indicators already stamp it.)
- 401 + `WWW-Authenticate: Bearer resource_metadata="…"` is the trigger; `scope` param SHOULD
  guide the client. RFC 9207 `iss` validation is new on the client side (AS-side: AuthKit).
- Tokens only in the `Authorization` header, never query strings. 401 = invalid/expired,
  403 = insufficient scope (step-up flow).
- Draft note: RS **SHOULD NOT** advertise `offline_access` in PRM `scopes_supported` /
  challenge `scope` (refresh is not a resource requirement — clients add it themselves from
  AS metadata). Our current challenge includes it → trim in Stage 1 (cosmetic, not blocking).

### 3.2 Per-platform requirements

| Requirement | Claude.ai (hosted web/desktop/mobile) | ChatGPT (connectors→"apps", dev mode) | Le Chat (custom MCP connectors) |
|---|---|---|---|
| Trigger | **401 required** — WWW-Authenticate hint on a 200 is ignored | 401 + WWW-Authenticate; also per-tool `securitySchemes` (`noauth`+`oauth2`) and `_meta["mcp/www_authenticate"]` | 401 + WWW-Authenticate; auth method auto-detected on add |
| PRM | `resource` must match the user-entered URL exactly; `authorization_servers[0]` used, **no fallback** | PRM at well-known path; `resource` + `authorization_servers` | RFC 9728 discovery |
| Registration | DCR (⚠ new client **per connection** — proliferation), **CIMD preferred** (AS must advertise `client_id_metadata_document_supported: true` + `"none"` in `token_endpoint_auth_methods_supported`), or Anthropic-held credentials via mcp-review@anthropic.com | **CIMD preferred** (`client_id` = HTTPS metadata URL; `none` or `private_key_jwt`); DCR supported (registers once per connector instance, reuses) | OAuth 2.1 **with DCR** |
| Redirect URIs | `https://claude.ai/api/mcp/auth_callback` (hosted); Claude Code = loopback, port-agnostic localhost | `https://chatgpt.com/connector/oauth/{callback_id}` (+ legacy `connector_platform_oauth_redirect`) | registered during DCR |
| PKCE | S256 always; AS must advertise `code_challenge_methods_supported:["S256"]` | S256 required | OAuth 2.1 (S256) |
| Tokens | form-encoded token endpoint; reactive refresh on 401 + proactive ≤5 min before expiry; **refresh-token rotation required** for public clients; discovery/token ≤10 s, refresh ≤30 s; AS reachable from Anthropic egress `160.79.104.0/21` | RFC 8707 `resource` echoed — RS must validate `aud`; OIDC scopes requested by default if advertised; optional mTLS client cert (SAN `mtls.prod.connectors.openai.com`) authenticates ChatGPT itself | HTTPS + valid TLS; per-user tokens (workspace-scoped connector, each member authorizes) |
| No-auth mode | Authless supported; **partial-auth experimental** | Mixed per-tool `noauth`+`oauth2` fully supported ← best fit for our freemium | Supported (auto-detect) |

Sources: modelcontextprotocol.io spec (2025-11-25 + draft authorization), claude.com/docs
connectors authentication, support.claude.com custom-connector articles,
developers.openai.com Apps SDK auth + MCP docs, docs.mistral.ai Le Chat MCP connectors,
workos.com/docs/authkit/mcp.

### 3.3 WorkOS AuthKit fit (what we get without writing an AS)

- Spec-compatible OAuth 2.1 AS ("Connect"): discovery, **DCR toggle**, **CIMD toggle**
  (their docs call CIMD "the current standard as of Nov 2025"), PKCE, consent, token issuance.
- **Resource Indicators**: register `https://dchub.cloud/mcp` → tokens carry matching `aud`
  (exactly what `DCHUB_WORKOS_AUD_ENFORCE` checks today).
- RS obligations = serve PRM + validate JWT via JWKS — both already implemented.
- Compatibility fallback for pre-PRM clients: proxy `/.well-known/oauth-authorization-server`
  to AuthKit metadata (we can add this to the CF worker in Stage 1 — metadata only, no auth code).
- Custom domain (`auth.dchub.cloud`) supported → consent page stops showing a raw
  `*.authkit.app` sandbox URL.

---

## 4. Identity merge: anonymous → keyed → OAuth (no history loss)

Principle: **one durable identity per agent-human pair; a key is never orphaned, never
downgraded, and history is unioned via alias, not rewritten.**

Today's gap: `/api/v1/oauth/identity` mints `dch_oauth_<HMAC(sub)>` unconditionally. An agent
that already carried a `dch_live_` (paid) or bound `dch_trial_` key and then OAuths would fork
into a fresh free identity — losing tier, history, and attribution. The fix is a backend-only
**merge ladder** inside that endpoint (Stage 2; ~40 lines + one table):

1. **Alias hit:** new table `oauth_identities (oauth_iss, oauth_sub, api_key, created_at,
   merged_via)` — if `(iss,sub)` already maps to a key, return that key. (Replaces the
   implicit determinism-as-identity with an explicit, re-pointable mapping; the deterministic
   HMAC key remains the default target so existing `dch_oauth_` rows keep working — backfill
   aliases for them in the same migration.)
2. **Verified-email merge:** if the OAuth email (JWT claim or Management API — IdP-verified,
   never client-supplied) matches `mcp_dev_keys.email` on an ACTIVE key, bind the alias to
   that key and return it. Precedence: paid (`dch_live_`) > highest tier > most-recently-used.
   The paid key keeps its tier and Stripe linkage → **a payer who OAuths later keeps
   everything**. Guard: merge is get-only — never mutate the target key's tier/email here.
3. **Fresh mint (fallback = today's behavior):** deterministic `dch_oauth_` key, tier `free`,
   plus an alias row.

Notes:
- **Anon → OAuth:** anonymous `dch_trial_` sessions are per-session by design; when the same
  human OAuths, step 2 catches them only if they previously ran `bind_email`. Unbound trial
  history is intentionally not merged (nothing reliable to join on; IP/session joins are
  forbidden by the attribution canon — `session_id` is never an identity).
- **OAuth → paid:** the Stripe webhooks mint `dch_live_` keyed by email. Because OAuth
  captures a verified email, the payer's later OAuth resolves onto the `dch_live_` key via
  step 2. The inverse enhancement (webhook upgrades the *aliased* key's tier instead of
  minting a new key) is desirable but deferred to the onboarding-consolidation track
  (PR #1416 family) — Open Question #4.
- **Attribution:** `mcp_calls_identity` rows are keyed by api_key; the alias table gives
  analytics a canonical-key view (`JOIN oauth_identities`) so reuse/retention dashboards can
  union pre- and post-OAuth history without touching write paths.
- **Security:** merge only on IdP-verified email; never on unverified `bind_email` input alone
  (an attacker controlling an email at the IdP is Open Question #4's confirmation-step debate).

---

## 5. Rollout plan — 4 shippable stages (additive; X-API-Key path untouched at every stage)

Every stage has a kill switch and leaves the previous stage's behavior recoverable by env
flip alone. No stage modifies the X-API-Key chain.

### Stage 0 — Coherence hotfix (½ day, config/env only, no code)
- Resolve §2.2: **either** set `DCHUB_OAUTH_CHALLENGE_DISABLE=1` (recommended interim — back
  to working anonymous connect while Stages 1–2 land) **or** accept broken keyless Claude.ai
  web adds until Stage 2 arms. Owner decision (Open Question #2).
- Re-establish a git source-of-truth for the live `dchub-oauth-meta` worker (pull current
  script via CF API into `workers/dchub-oauth-meta.js` in this repo).
- Verification: keyless `initialize` with `clientInfo.name=claude-ai` → 200; keyed → 200;
  real Claude.ai custom-connector add succeeds anonymously.

### Stage 1 — Production AS + ONE metadata truth (1–2 days, dashboard/env/worker; no auth code)
- WorkOS: promote/stand up the **production** AuthKit environment; custom domain
  `auth.dchub.cloud`; enable **CIMD + DCR** (CIMD for Claude/ChatGPT, DCR for Le Chat);
  Resource Indicator `https://dchub.cloud/mcp`; allowlist redirect URIs
  (`https://claude.ai/api/mcp/auth_callback`, `https://chatgpt.com/connector/oauth/*` +
  legacy, DCR self-registers Le Chat's); JWT template adds `email` claim (kills the
  Management-API fallback hop); confirm refresh-token rotation on (Claude requirement).
- Envs, updated in lockstep: gateway `WORKOS_AUTHKIT_DOMAIN=https://auth.dchub.cloud`;
  backend same + `MCP_OAUTH_ADVERTISE=1`; CF worker PRM `authorization_servers:
  ["https://auth.dchub.cloud"]` + purge.
- Kill the three-manifests problem: frontend `_worker.js` PRM block deleted or slaved to the
  worker JSON; Flask stale RFC 8414 doc + 501 stubs retired (worker may instead proxy
  `/.well-known/oauth-authorization-server` → AuthKit metadata for pre-PRM clients).
- Trim `offline_access` from the challenge `scope` (spec-draft alignment; Claude adds it from
  AS metadata itself).
- Verification: PRM (both well-known variants + Flask path) byte-agree; AS discovery reachable
  from Anthropic egress; token endpoint <10 s.

### Stage 2 — Identity merge + arm for Claude.ai (2–3 days: one backend route + one table + flag flip)
- Backend: `oauth_identities` table + merge ladder (§4) in `/api/v1/oauth/identity`
  (additive; deterministic mint stays as fallback; migration backfills aliases for existing
  `dch_oauth_` rows). This is the only new auth-adjacent code in the whole plan.
- Arm: unset `DCHUB_OAUTH_CHALLENGE_DISABLE`. Challenge remains scoped to the Claude.ai web
  cohort only (clientInfo `claude-ai` / UA `Claude-User`); every other caller stays 200-anonymous.
- Watch: gateway `[oauth]` logs for `aud mismatch` (= Resource Indicator misconfig — fix in
  WorkOS, never via `DCHUB_WORKOS_AUD_ENFORCE=0`), 401-storm symptoms, `mcp_dev_keys`
  `dch_oauth_` growth, `last_used_at` return-week movement.
- Instrument the funnel: challenge-issued → PRM fetched → token verified → key resolved →
  tool-call — as reach-dashboard columns (north star: distinct agents/wk; target metric:
  multi-day reuse off the 1.2% floor).
- Verification matrix (per-platform, §6): Claude.ai add → consent → durable key → disconnect →
  reconnect next day → SAME key.

### Stage 3 — ChatGPT + Le Chat + listings (2–4 days, mostly validation + review cycles)
- ChatGPT: do **NOT** 401-challenge ChatGPT (anonymous stays the reach engine); instead adopt
  per-tool `securitySchemes` (`noauth` + `oauth2`) in tool metadata so linking is *offered*
  without breaking anonymous use — the best-fit pattern for our freemium gate. Validate in
  developer mode; optional later: verify OpenAI's mTLS client cert to platform-attribute
  ChatGPT traffic cryptographically.
- Le Chat: validate DCR + consent flow end-to-end (workspace-scoped, per-user tokens).
- Listings: Claude Directory entry is currently "No auth needed" — re-listing as OAuth
  triggers re-review (Open Question #3). Consider CIMD or Anthropic-held credentials to avoid
  DCR client proliferation at AuthKit.
- Effort total: **~6–10 engineer-days across 2–3 calendar weeks** (platform review cycles
  dominate Stage 3).

---

## 6. Per-platform validation matrix (run after each stage flip)

| Check | Claude.ai web | ChatGPT | Le Chat | Claude Code / keyed CLI (regression) |
|---|---|---|---|---|
| Keyless add/connect | Stage 0: anonymous 200 · Stage 2: 401 → OAuth → consent → connected | anonymous 200 (never challenged); OAuth offered via securitySchemes | add connector → auto-detect → DCR → consent → connected | anonymous 200, `claim_free_key` works |
| PRM fetch | both `/.well-known/oauth-protected-resource` and `…/mcp` return AS list | same | same | n/a |
| Token → identity | Bearer JWT verified; `aud=https://dchub.cloud/mcp`; resolves to durable key | same | same | X-API-Key path byte-identical |
| Durability | disconnect → reconnect next day → SAME `dch_oauth_`/merged key; history unioned | same | same | keyed history unchanged |
| Merge | pre-existing `dch_live_` (same email) → OAuth resolves onto the PAID key, tier intact | same | same | n/a |
| Failure drills | AS down → 401 flow fails gracefully, keyed callers unaffected; token expiry → refresh (rotation) works | expired token → 401 → relink | revoke in Le Chat → next call 401 | kill switches: `DCHUB_OAUTH_CHALLENGE_DISABLE=1`, `DCHUB_WORKOS_OAUTH_ENABLED=0` |

---

## 7. Open questions for the owner

1. **Which WorkOS environment is production?** Gateway was proven against
   `safe-countryside-77-sandbox.authkit.app`; `beloved-stream-52.authkit.app` appears in the
   frontend worker and 07-03 notes. Pick one, budget for it (AuthKit pricing/MAU at expected
   agent volume), and stand up `auth.dchub.cloud`. (Railway envs were unverifiable in this
   session — CLI/MCP auth expired; live probes above are the ground truth used.)
2. **Interim posture (Stage 0):** was re-arming the 401 challenge with an empty PRM
   intentional? Recommended: disable the challenge until Stage 2; confirm.
3. **Claude Directory:** keep the listing authless (max reach, OAuth only for direct custom-
   connector adds) or re-submit as OAuth (re-review risk; partial-auth is still experimental
   on Claude)? This decision gates whether the challenge can ever target directory traffic.
4. **Merge policy for paid keys:** auto-merge on IdP-verified email match (proposed), or
   require an explicit confirmation step (protects against an attacker who controls the
   email account at the IdP; costs conversion)? Also: should Stripe webhooks upgrade an
   aliased OAuth key's tier in place instead of minting a parallel `dch_live_`?
5. **Fate of `oauth.mjs` (self-rolled AS):** delete after AuthKit is proven, or keep dormant
   as a DR fallback (cost: dead code + two guards to keep byte-identical)?
6. **Refresh/token lifetimes:** confirm AuthKit access-token TTL + refresh rotation settings
   meet Claude's ≤10 s token / ≤30 s refresh latency and rotation requirements at our scale.
7. **Protocol version:** we advertise `2025-06-18`; current spec is `2025-11-25` (CIMD,
   RFC 9207). Gateway SDK/protocol upgrade is orthogonal to this ADR but should be scheduled —
   CIMD is AS-side (AuthKit handles it), so nothing here blocks on the upgrade.
8. **Analytics unification:** add the alias-canonicalized identity view to the reach
   dashboard so pre/post-OAuth history reads as one agent (agents = distinct identities,
   never session_ids).

---

## 8. Consequences

- **Positive:** durable identity for the three hosted-web platforms with ~40 lines of new
  backend code (merge ladder) — everything else is configuration of already-shipped, already-
  live-tested components. Tier/limits/paywall/attribution untouched because tokens resolve to
  ordinary api_keys at the seam. Structural fix for the 1.2% reuse rate; retention becomes
  measurable per identity (`created_at` vs `last_used_at` ISO-week).
- **Negative / accepted:** WorkOS becomes an availability + cost dependency on the Claude.ai
  auth path (keyed and anonymous paths unaffected by an AuthKit outage); three surfaces
  (CF worker, Flask, gateway env) must stay in lockstep — mitigated by Stage 1's
  single-source-of-truth consolidation; Claude Directory re-review risk if/when the listing
  changes.
- **Never:** the X-API-Key validation chain is not modified, deprecated, or challenged for
  any non-Claude.ai cohort. Anonymous access remains the reach engine.
