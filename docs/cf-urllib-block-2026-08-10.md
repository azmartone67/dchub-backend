# Cloudflare 403s the default `urllib` User-Agent on the public keyless API

**Date filed:** 2026-08-10
**Owner:** infra
**Status (2026-08-22):** ROOT-CAUSED FOR REAL — the **Cloudflare Pages pipeline's own Browser
Integrity Check**, not this zone's. Evidence: `dchub-frontend.pages.dev` 403s `Python-urllib`/`libwww-perl`
directly; `api.dchub.cloud` (zone worker → Railway, not Pages) answers 200; zone `browser_check` has been
**off since 2026-08-13** (`GET /zones/{id}/settings/browser_check`); configuration rules, custom rules, page
rules, snippets, UA blocking, AI Crawl Control and both workers' source verified clean in the dashboard; CF's
per-request log (`httpRequestsAdaptive`) records the 403 as an ORIGIN response with `securityAction: unknown`.
No zone setting can reach it; only a zone Worker route in front of Pages bypasses it (why `/mcp*` and
`/.well-known/*` pass). **Owner decision 2026-08-22: do not reroute production paths for this; the probe is now
a GUARD for real clients (python-requests/Go/node-fetch/curl/Wget) and a GAUGE for bare urllib.**
The 2026-08-10 text below diagnosed the zone's BIC, which was on at the time and is not the blocker now.

**Status (2026-08-10, superseded):** ROOT-CAUSED (Browser Integrity Check, confirmed from CF firewall events).
Remediation is a **Cloudflare Configuration Rule a human must make** — no available token
can write it. A WAF custom-rule fix was attempted live, proven ineffective, and reverted
(see below). Regression guard is shipped and currently RED (correctly).
**Zone:** `dchub.cloud` / `1cb22dda8d50546d6edf0c09a8be5128` (Pro)

---

## Summary

Any HTTP request whose `User-Agent` **starts with** `Python-urllib` gets
`HTTP 403` with body `error code: 1010` on **every public path** of
`dchub.cloud`. That is the exact three-line script an analyst or an agent writes
against an API we deliberately publish keyless so third parties can verify our
numbers.

**This is not application code.** It is not `worker.js`, not the Flask backend,
not a key requirement, and not path-specific. It is a Cloudflare edge
configuration on this zone, and it must be fixed there.

## Evidence

The 403 never reaches our code. A permitted UA gets the worker; a blocked one
gets nothing but Cloudflare:

```
$ curl -sSI -A "curl/8.7.1"          https://dchub.cloud/api/v1/dcpi/scores/tokyo
x-dc-worker-version: 4.66.0-research-static-guard-2026-08-08
x-dc-hub-served-by: railway-primary
cf-cache-status: DYNAMIC

$ curl -sSI -A "Python-urllib/3.11"  https://dchub.cloud/api/v1/dcpi/scores/tokyo
HTTP/2 403
server: cloudflare
cf-ray: a2923e921d038809-PHX
        # ← no x-dc-worker-version, no x-dc-hub-*, no cf-cache-status
```

`Server-Timing: cfEdge;dur=2,cfOrigin;dur=0` on the 403 — the origin was never
contacted. Body is `error code: 1010`, Cloudflare's "banned based on your
browser's signature".

### It is only `Python-urllib`, and only as a prefix

Every UA in the brief was tested. Six of the seven **already work**:

| User-Agent | Status |
|---|---|
| `Python-urllib/3.11` | **403** |
| `python-requests/2.31.0` | 200 |
| `python-httpx/0.27.0` | 200 |
| `Go-http-client/2.0` | 200 |
| `node-fetch/1.0` | 200 |
| `axios/1.6.0` | 200 |
| `okhttp/4.12.0` | 200 |
| `curl/8.7.1`, curl default, `Mozilla/5.0`, `GPTBot/1.0`, `Claude-User` | 200 |

The match is **prefix-anchored on the UA string**, not a substring and not a
heuristic:

| User-Agent | Status | |
|---|---|---|
| `Python-urllib` | **403** | prefix alone is enough |
| `Python-urllib/3` | **403** | |
| `MyTool Python-urllib/3.14` | 200 | same token, not at the start → allowed |
| `Pythonurllib/3.14` | 200 | hyphen required |
| `DCHub-urllib/3.14` | 200 | |

### ★ CONFIRMED: it is Browser Integrity Check (corrected 2026-08-10, later same day)

An earlier revision of this doc argued from the prefix-anchored behaviour that this
was a hand-written rule rather than BIC. **That was wrong.** Cloudflare's own
firewall event log names the source directly:

```
$ # GraphQL firewallEventsAdaptive, action:"block", last 15 min
2026-08-10T23:00:10Z src=bic rule=bic path=/robots.txt      ua=Python-urllib/3.14
2026-08-10T23:00:10Z src=bic rule=bic path=/api/v1/stats    ua=Python-urllib/3.14
2026-08-10T22:54:03Z src=bic rule=bic path=/api/v1/dcpi/... ua=Python-urllib/3.14
```

`source=bic`, `rule=bic`. It is Browser Integrity Check, zone-wide.
Read it with `CF_ANALYTICS_READ_TOKEN` (the WAF/ruleset tokens cannot).

### ★★★ A WAF custom-rule `skip` with `products:["bic"]` DOES NOT WORK — measured

This is the important operational finding, and it contradicts what
`docs/cf-waf-bypass-self-probes-2026-06-06.md` assumes.

**BIC is evaluated BEFORE the `http_request_firewall_custom` phase**, so by the time
a custom rule could skip it, the request is already blocked. Tried live on this zone
and reverted:

| attempt | result |
|---|---|
| new custom rule, `products:["bic","uaBlock"]` | still 403 |
| + `phases:[http_ratelimit, http_request_firewall_managed, http_request_sbfm]`, `ruleset:"current"` | still 403 |
| + all seven products — byte-identical action_parameters to the working `/mcp` rule | **still 403** |

All three were reverted; the ruleset was restored byte-identical to its pre-change
snapshot and verified. **Do not add a WAF custom rule for this** — it cannot work,
and (because such a rule also skips `waf` and `rateLimit`) it is a net security loss
on the public surface in exchange for nothing.

Corollary worth knowing: the `bic` entries in the three existing self-probe/crawler
skip rules are **cosmetic**. They are not what makes `DCHub-*` probes work.

### ★ Why `/mcp` is exempt, and what the real lever is

`/mcp` returns 200 to `Python-urllib` while every other path 403s. Since a custom-rule
`bic` skip provably does nothing, rule #3 ("Allow MCP traffic") is not what exempts it.
The remaining mechanism that CAN disable BIC per-path is a **Configuration Rule**
(ruleset phase `http_config_settings`), which runs early, before the security phase.
So there is almost certainly already a Configuration Rule turning BIC off for `/mcp` —
and **that is the lever this fix needs too.**

This could not be confirmed from here: **none of the four Cloudflare tokens in Railway
can read `http_config_settings`** (`CF_WAF_EDIT_TOKEN`, `CF_RULESET_TOKEN`,
`CLOUDFLARE_API_TOKEN` → "request is not authorized"; `CF_ANALYTICS_READ_TOKEN` →
"Authentication error"). `CF_WAF_EDIT_TOKEN` can read and PUT the *firewall* ruleset
but nothing else, and rejects `POST .../rules` with `10405 Method not allowed for this
authentication scheme` — full-ruleset `PUT` is the only write verb it accepts.

### Blast radius — everything we point third parties at

All 200 under `curl`, all 403 under `Python-urllib`:

| Path | urllib | allowed UA |
|---|---|---|
| `/api/v1/dcpi/scores/tokyo` | **403** | 200 `application/json` |
| `/api/v1/dcpi/methodology` | **403** | 200 `application/json` |
| `/api/v1/stats` | **403** | 200 `application/json` |
| `/llms.txt` | **403** | 200 `text/plain` |
| `/sitemap-dcpi.xml` | **403** | 200 `application/xml` |
| `/robots.txt` | **403** | 200 `text/plain` |
| `/api/v1/health`, `/ai` | **403** | 200 |

`/robots.txt` and `/llms.txt` being 403 to a stdlib fetch is the worst of these:
those are the files that tell crawlers and agents what we permit.

---

## Remediation — exact steps for a human

The fix is a **Configuration Rule**, not a WAF custom rule and not a code change.
It could not be applied from here: no available token can touch
`http_config_settings` (see above).

### Option A — dashboard (recommended; this is the whole fix)

Cloudflare dashboard → `dchub.cloud` → **Rules → Configuration Rules** →
**Create rule**.

- **Name:** `Allow programmatic clients on public keyless API (2026-08-10)`
- **Setting to override:** **Browser Integrity Check** → **Off**
- **Expression:**

```
(http.request.method in {"GET" "HEAD"})
and (starts_with(http.request.uri.path, "/api/v1/dcpi/")
     or starts_with(http.request.uri.path, "/sitemap")
     or starts_with(http.request.uri.path, "/.well-known/")
     or http.request.uri.path in {"/api/v1/stats" "/api/v1/health" "/api/v1/version"
                                  "/llms.txt" "/llms-full.txt" "/robots.txt" "/ai" "/agent"})
```

★ **Scope it by PATH only — do not add a User-Agent clause.** BIC's whole job is to
judge the UA; a Configuration Rule that turns BIC off only for UAs we list would let
anyone claiming that UA past it, which is strictly worse than turning it off for the
public read paths outright. Method is pinned to GET/HEAD, and the paths are the
already-public keyless surface, so nothing gated is exposed. WAF managed rules, rate
limiting and security level all remain fully active on these paths — a Configuration
Rule changes only the one named setting.

While you are in there, check whether an existing Configuration Rule already turns
BIC off for `/mcp`. If so, the cleanest change is to widen that rule's expression
rather than add a second one.

### Option B — API

`scripts/cf_allow_programmatic_ua_public_api.sh` now targets the Configuration Rules
phase. It needs a token with **Zone → Config Rules → Edit** (no existing token has
it):

```bash
export CF_CONFIG_RULES_TOKEN="..."      # dash.cloudflare.com/profile/api-tokens
DRY_RUN=1 bash scripts/cf_allow_programmatic_ua_public_api.sh   # inspect first
bash scripts/cf_allow_programmatic_ua_public_api.sh
```

★ Its **write path is untested** — no token here could exercise it. The read/DRY_RUN
path was exercised. Run `DRY_RUN=1` first and check the payload.

### Option C — the blunt one, if you want it open now

Security → Settings → **Browser Integrity Check → Off**, zone-wide. This works
immediately and needs no new token, but drops BIC on every path including admin.
Only worth it as a deliberate, temporary step; prefer Option A.

### Step 3 — verify from the outside

Not from the dashboard, and not from the API response:

```bash
python3 scripts/check_public_api_programmatic_access.py --base https://dchub.cloud
```

It sends urllib's genuine default UA, cache-busts every request, prints
`cf-cache-status` per path, and exits non-zero on any 403.

## The guard

`scripts/check_public_api_programmatic_access.py`, armed by
`.github/workflows/public-api-programmatic-access.yml` (every 6h + on push +
dispatch). It is **RED right now**, correctly, and goes green when step 2 lands.

★ **It asserts it is not vacuous before reporting anything.** `main.py` line 1
imports `http_ua_default`, a global shim that forces a browser UA onto every
urllib/requests call in the backend — installed to dodge this very 403 on
outbound calls. If that shim, or any added `User-Agent` header, ever reached
this guard, it would send a browser UA, get 200 forever, and pass whether or not
the API was reachable. So the guard reads the UA off the opener it is about to
fetch with and exits **2** if it is not `Python-urllib/*`.

Mutation-tested three ways on 2026-08-10, all confirmed:

| | | |
|---|---|---|
| against live `dchub.cloud` (allowance absent — today's real state) | exit **1**, 6/6 blocked | goes red on the real defect |
| against an unblocked host, same real urllib UA | exit **0** | not hard-red |
| with `build_opener` patched to a browser UA | exit **2** | vacuity assertion fires |

The first row is the mutation test the brief asked for: the allowance is not
in place, so production *is* the "allowance removed" state, and the guard fails
against it. When step 2 lands, that row flips to exit 0 — and that flip is the
proof the fix worked.
