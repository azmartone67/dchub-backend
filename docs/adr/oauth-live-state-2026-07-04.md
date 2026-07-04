# ADR — Live OAuth / MCP Protected-Resource state (2026-07-04)

**Status:** Captured (read-only archaeology). No code, deploy, worker, or env
change was made. This ADR records the *as-deployed* state so the OAuth /
onboarding fix can be reasoned about against ground truth rather than against
git (which has drifted from what is live).

**Scope:** OAUTH STAGE-0 capture. Records (a) the three live discovery
endpoints, (b) the reproduced **401-challenge-vs-empty-PRM incoherence**, and
(c) the credential blocker that prevented mirroring the `dchub-oauth-meta` CF
worker source.

---

## TL;DR — what the live PRM state actually is

- **Both** Protected-Resource Metadata (PRM) documents advertise
  `authorization_servers: []` — i.e. RFC 9728 for "**no** authorization server;
  no OAuth required." The worker-served `.well-known` copy and the Flask-served
  `/api/v1` copy **agree** on this.
- **Yet** a keyless MCP `initialize` whose `clientInfo.name == "claude-ai"`
  gets **HTTP 401** with `WWW-Authenticate: Bearer resource_metadata="…/api/v1/
  oauth-protected-resource"` — a challenge that says "authenticate," pointing at
  a PRM that says "there is nothing to authenticate against."
- The 401 is gated **purely on `clientInfo.name`**. `cursor` and a generic
  `test-client` both get a clean **200** anonymous `initialize`. Only Claude.ai
  is singled out — and it is the one client sent into an **unsatisfiable**
  OAuth flow (challenge → PRM → empty `authorization_servers` → nowhere to sign
  in). This is the mechanism behind the "WorkOS login the customer cannot pass"
  onboarding dead-end.
- `.well-known/oauth-authorization-server` is a **live 404**, even though the
  Flask blueprint defines an RFC 8414 handler for it (the worker intercepts
  `.well-known/*` and hard-404s that path).

---

## Topology observed (three distinct components serve "OAuth"-shaped paths)

| Path | HTTP | Serving component (from headers) | `authorization_servers` |
|---|---|---|---|
| `GET /.well-known/oauth-protected-resource` | 200 | **CF worker** `dchub-oauth-meta` (CF-only headers; no railway markers) | `[]` |
| `GET /.well-known/oauth-authorization-server` | **404** | CF worker (`Not Found`, text/plain) | n/a |
| `GET /api/v1/oauth-protected-resource` | 200 | **Railway / Flask** (`x-dc-hub-served-by: railway-primary`, `x-dc-worker-version: 4.48.0-tier-aware-cache-dark-2026-07-03`) | `[]` |
| `POST /mcp` (keyless, `clientInfo.name=claude-ai`) | **401** | **Express MCP passthrough worker** (`x-powered-by: Express`, `x-dc-worker-version: 4.9.24-semantic-search-passthrough`, `x-dc-hub-source: worker-mcp-passthrough`) | issues the challenge |
| `POST /mcp` (keyless, `clientInfo.name=cursor` or `test-client`) | 200 | same passthrough worker | anonymous success |

Three different code units touch this flow: the `dchub-oauth-meta` edge worker
(discovery `.well-known/*`), the Express MCP passthrough worker (the `/mcp`
handshake + 401 gate), and Flask (`/api/v1` PRM). None of them enforces a real
authorization server.

---

## Endpoint captures (verbatim, curl, 2026-07-04 ~15:25 UTC)

### 1. `GET https://dchub.cloud/.well-known/oauth-protected-resource` → 200
Selected response headers:
```
HTTP/2 200
content-type: application/json; charset=utf-8
cache-control: no-store
access-control-allow-origin: *
server: cloudflare
speculation-rules: "/cdn-cgi/speculation"
```
(No `x-dc-hub-served-by` / `x-dc-worker-version` / `x-railway-*` → served by
the CF worker, not Railway.)
Body:
```json
{
  "resource": "https://dchub.cloud/mcp",
  "resource_name": "DC Hub Intelligence MCP Server",
  "resource_documentation": "https://dchub.cloud/integrations/mcp",
  "authorization_servers": [],
  "bearer_methods_supported": ["header"],
  "scopes_supported": ["openid", "profile", "email", "offline_access"],
  "mcp_protocol_version": "2025-06-18"
}
```

### 2. `GET https://dchub.cloud/.well-known/oauth-authorization-server` → 404
```
HTTP/2 404
content-type: text/plain; charset=utf-8
cache-control: no-store
server: cloudflare
```
Body: `Not Found`

RFC 8414 authorization-server metadata is **not reachable** at the well-known
path in production. `routes/mcp_oauth_2025_06_18.py` defines this route, but the
`dchub-oauth-meta` worker intercepts `.well-known/*` and returns a hard 404
before Flask is reached.

### 3. `GET https://dchub.cloud/api/v1/oauth-protected-resource` → 200
Selected response headers:
```
HTTP/2 200
content-type: application/json
x-dc-hub-served-by: railway-primary
x-dc-hub-backend: railway
x-dc-worker-version: 4.48.0-tier-aware-cache-dark-2026-07-03
cf-cache-status: HIT
cache-control: private, max-age=0, must-revalidate
```
Body (full Flask shape — note it is a superset of the worker's `.well-known`
copy: adds `resource_policy_uri`, `resource_tos_uri`, `enterprise_contact`,
`computed_at`, `mcp_capabilities`, `mcp_protocol_versions_supported`,
`resource_signing_alg_values_supported`, `self_serve_alternative`,
`tier_with_oauth`):
```json
{"authorization_servers":[],"bearer_methods_supported":["header"],"computed_at":"2026-07-04T14:47:15.850807Z","enterprise_contact":"api@dchub.cloud","mcp_capabilities":{"prompts":{"list_changed":false},"resources":{"list_changed":false,"subscribe":false},"tools":{"list_changed":true}},"mcp_protocol_version":"2025-06-18","mcp_protocol_versions_supported":["2024-11-05","2025-06-18"],"resource":"https://dchub.cloud/mcp","resource_documentation":"https://dchub.cloud/integrations/mcp","resource_name":"DC Hub Intelligence MCP Server","resource_policy_uri":"https://dchub.cloud/terms","resource_signing_alg_values_supported":["RS256","ES256"],"resource_tos_uri":"https://dchub.cloud/terms","scopes_supported":["openid","profile","email","offline_access"],"self_serve_alternative":"Use X-API-Key header instead (free + dev tier).","tier_with_oauth":"enterprise"}
```

---

## The 401-challenge-vs-empty-PRM incoherence — reproduced and verified

The reviewer reproduced this; I re-verified it independently.

### Trigger (keyless POST /mcp, `clientInfo.name=claude-ai`)
```
POST https://dchub.cloud/mcp
Content-Type: application/json
Accept: application/json, text/event-stream
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{
  "protocolVersion":"2025-06-18","capabilities":{},
  "clientInfo":{"name":"claude-ai","version":"1.0.0"}}}
```
Response:
```
HTTP/2 401
www-authenticate: Bearer resource_metadata="https://dchub.cloud/api/v1/oauth-protected-resource", scope="openid profile email offline_access"
x-powered-by: Express
x-dc-worker-version: 4.9.24-semantic-search-passthrough
x-dc-hub-source: worker-mcp-passthrough

{"jsonrpc":"2.0","error":{"code":-32001,"message":"Authorization required — sign in to DC Hub to continue."},"id":1}
```

### Is the referenced PRM valid RFC 9728 JSON? — YES, but a dead end
The `WWW-Authenticate` points to `https://dchub.cloud/api/v1/oauth-protected-
resource`. That URL returns **HTTP 200 valid JSON** with the required RFC 9728
`resource` member present (capture #3 above). It is structurally valid — **but
its `authorization_servers` array is empty.** A spec-compliant client (Claude.ai)
follows RFC 9728, reads the metadata, finds **no** authorization server, and has
**nowhere to send the user to obtain the demanded Bearer token.** The challenge
is unsatisfiable. This is the incoherence: **challenge demands a token that the
referenced discovery document makes it impossible to obtain.**

### The gate is `clientInfo.name`, not User-Agent — control matrix
| `clientInfo.name` | User-Agent | Result |
|---|---|---|
| `claude-ai` | `claude-ai/1.0` | **401** + WWW-Authenticate (challenge) |
| `claude-ai` | `generic-ua/1.0` | **401** + WWW-Authenticate (challenge) |
| `cursor` | `cursor/1.0` | **200** anonymous `initialize` (full capabilities) |
| `test-client` | `python-httpx/0.27` | **200** anonymous `initialize` (full capabilities) |

Conclusion: the passthrough worker singles out `clientInfo.name == "claude-ai"`
and forces **only Claude.ai** into an OAuth challenge, while every other MCP
client is served anonymously. Since the challenge dead-ends at an empty-AS PRM,
Claude.ai is effectively the one client that **cannot** complete onboarding
via the discovery flow. (X-API-Key still works — the `initialize` accepts a key
header — but the unauthenticated discovery path Claude.ai auto-runs cannot.)

---

## Drift: live worker vs git source

The git-tracked worker `dchub-frontend/_worker.js` (repo
`azmartone67/dchub-frontend`) contains a handler that emits the **same 7-field
shape, same field order** as the live `.well-known/oauth-protected-resource`
body — **except** it sets:
```js
authorization_servers: ['https://beloved-stream-52.authkit.app']   // git HEAD
```
Live returns `authorization_servers: []`. So the deployed `dchub-oauth-meta`
worker is **behind / diverged from** that git source: the WorkOS-AuthKit
advertisement that exists in git is **not live**. Had that git version been
deployed, the 401 → PRM chain would resolve to a real AuthKit AS. It has not
been; live serves the empty array. This confirms `dchub-oauth-meta` is not
mirrored by / in sync with the committed source.

Corroborating repo signal: `routes/mcp_oauth_2025_06_18.py` gates the AuthKit
advertisement behind `MCP_OAUTH_ADVERTISE` (default OFF) precisely because
"advertising an authorization_server makes Claude.ai/Cursor START an OAuth
sign-in the moment the connector is added" while `/mcp` actually accepts
anonymous + X-API-Key. The Flask `/api/v1` PRM honors that gate (empty AS
live); the live edge worker also serves empty AS.

---

## Credential blocker — why `dchub-oauth-meta` source was NOT pulled

The task's canonical pull path (`railway variables … CLOUDFLARE_API_TOKEN` →
CF `workers/scripts/dchub-oauth-meta` multipart) could not run on this host:

- **Railway CLI** `railway variables --service dchub-backend` → OAuth refresh
  `invalid_grant`, then `Unauthorized. Please run railway login again.`
- **Railway MCP** (`list_projects` / `list_variables`) → `Unauthorized`.
- **Stored Railway token** (`~/.railway/config.json` accessToken) → GraphQL
  `Not Authorized`; refresh token revoked.
- **wrangler OAuth** (`~/.wrangler/config/default.toml`, valid to 17:49Z)
  authenticates but its user is **not a member** of CF account
  `4bb33ec40ef02f9f4b41dc97668d5a52`:
  `GET /accounts` → `[]`;
  `GET /accounts/4bb33ec4…` → `9109 Unauthorized to access requested resource`;
  `GET …/workers/scripts/dchub-oauth-meta` → HTTP 403 `10000 Authentication error`.

The worker's **behavior** is fully captured in
`infra/cf-workers/dchub-oauth-meta.captured-2026-07-04.js` (verbatim live
responses), and that file contains the exact command to extract the real JS
module once a valid CF credential for account `4bb33ec4…` is available. **No
change was made to reproduce this — it is a live production observation.**

---

## Implications (recorded, not acted on)

1. The live PRM is **coherent internally** (both PRMs say "no AS") but
   **incoherent with the /mcp 401** (which demands auth for `claude-ai` and
   points at that no-AS PRM). Any fix must reconcile the passthrough worker's
   `claude-ai` 401 gate with the PRM's empty `authorization_servers`.
2. Two remedies are internally consistent, opposite directions:
   (a) **Stop challenging** `claude-ai` at `/mcp` (return 200 anonymous like
   every other client) so behavior matches the "no auth" PRM; or
   (b) **Actually advertise** a working authorization server in **both** PRMs
   AND enforce it at `/mcp` for all clients (deploy the git AuthKit version +
   set `MCP_OAUTH_ADVERTISE=1`) — but only after the WorkOS login the customer
   must pass is verified end-to-end.
3. `.well-known/oauth-authorization-server` returning 404 while Flask defines
   it means the RFC 8414 metadata is unreachable at the standard path; clients
   relying on it (rather than the resource_metadata pointer) get nothing.
4. `dchub-oauth-meta` must be brought under version control / lockstep with
   `routes/mcp_oauth_2025_06_18.py` — the live/git drift above is exactly the
   failure mode the source comments warn about ("must be updated in lockstep").

_This ADR is descriptive. It prescribes nothing beyond recording live state._
