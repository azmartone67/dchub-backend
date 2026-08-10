# Cloudflare 403s the default `urllib` User-Agent on the public keyless API

**Date filed:** 2026-08-10
**Owner:** infra
**Status:** DIAGNOSED — remediation is a **Cloudflare dashboard/API change a human must make**. Regression guard is shipped and currently RED (correctly).
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

### ★ It is almost certainly a hand-written rule, not Browser Integrity Check

Error 1010 is emitted by both Browser Integrity Check (`bic`) and User Agent
Blocking (`uaBlock`), so the error code alone does not identify it. But BIC is a
*heuristic* that flags automated signatures broadly — it would not clear
`python-requests`, `Go-http-client`, `okhttp`, `node-fetch` and `axios` while
singling out `Python-urllib` on an exact prefix. **A heuristic does not have
that shape. A hand-written rule does.**

Most likely location, in order:

1. **Security → WAF → Tools → User Agent Blocking** — an entry matching
   `Python-urllib*`, action Block. This returns 1010 and matches the observed
   prefix behaviour exactly.
2. A **custom WAF rule** in `http_request_firewall_custom` with a `block`
   action on `starts_with(http.user_agent, "Python-urllib")`.
3. Browser Integrity Check (Security → Settings) — least likely, per above.

### ★ The existing self-probe skip rule does NOT rescue it

`docs/cf-waf-bypass-self-probes-2026-06-06.md` documents a skip rule whose
expression includes `lower(http.user_agent) contains "dchub"` and whose products
include `bic` and `uaBlock`. That clause is **not behaving as documented**:

```
dchub-urllib/3.14                 -> 200     (starts_with "dchub-"  → allowed)
Python-urllib/3.14 dchub          -> 403     (contains  "dchub"     → still blocked)
Python-urllib/3.14 DCHubHealer    -> 403     (contains  "DCHubHealer" → still blocked)
```

Either the live rule no longer carries the `contains` clauses, or a `block` rule
is evaluated **before** the skip and terminates first (in a Cloudflare ruleset,
an earlier `block` wins — a later `skip` is never reached).

**Consequence for whoever fixes this:** adding UAs to the *existing* skip rule
may not be enough. Find the block first. If a block rule exists, either narrow
it or place the new skip rule ahead of it.

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

The available Railway `CLOUDFLARE_API_TOKEN` is read-only and cannot even
*read* the rulesets (verified: `403 code 10000 "Authentication error"` on
`/rulesets/phases/.../entrypoint`, and `9109 "Unauthorized"` on
`/settings/browser_check`). So this could not be done or verified from here.

### Step 1 — find the block (do this first; do not skip to step 2)

In the Cloudflare dashboard for `dchub.cloud`:

1. **Security → WAF → Tools → User Agent Blocking.** Look for any entry
   matching `Python-urllib`. This is the most likely culprit.
2. **Security → WAF → Custom rules.** Look for a `block` rule referencing
   `http.user_agent`. Note its **position** — anything above the allow rule wins.
3. **Security → Settings → Browser Integrity Check.** Note whether it is on.

### Step 2 — apply the allowance

**Option A — dashboard.** If step 1 found a User Agent Blocking entry for
`Python-urllib`: delete it, or if it was added for a real abuse reason, keep it
and add the custom rule below *above* it.

Security → WAF → Custom rules → **Create rule**:

- **Name:** `Allow programmatic clients on public keyless API (2026-08-10)`
- **Action:** `Skip` → check only **Browser Integrity Check** and
  **User Agent Blocking**. Leave WAF, Rate Limiting, Security Level unchecked.
- **Placement:** **First**.
- **Expression** (Edit expression):

```
(http.request.method in {"GET" "HEAD"})
and (starts_with(http.request.uri.path, "/api/v1/dcpi/")
     or starts_with(http.request.uri.path, "/sitemap")
     or starts_with(http.request.uri.path, "/.well-known/")
     or http.request.uri.path in {"/api/v1/stats" "/api/v1/health" "/api/v1/version"
                                  "/llms.txt" "/llms-full.txt" "/robots.txt" "/ai" "/agent"})
and (starts_with(http.user_agent, "Python-urllib")
     or starts_with(http.user_agent, "python-requests")
     or starts_with(http.user_agent, "python-httpx")
     or starts_with(http.user_agent, "httpx")
     or starts_with(http.user_agent, "Go-http-client")
     or starts_with(http.user_agent, "node-fetch")
     or starts_with(http.user_agent, "axios")
     or starts_with(http.user_agent, "okhttp")
     or starts_with(http.user_agent, "curl")
     or starts_with(http.user_agent, "Wget")
     or starts_with(http.user_agent, "Java/")
     or starts_with(http.user_agent, "Ruby")
     or starts_with(http.user_agent, "PostmanRuntime"))
```

**Option B — script.** `scripts/cf_allow_programmatic_ua_public_api.sh` writes
exactly that rule via the rulesets API, idempotently, and prints the existing
rules in evaluation order so you can see a competing `block`. It needs a token
with `Zone > Zone WAF > Edit`:

```bash
export CF_WAF_EDIT_TOKEN="..."          # dash.cloudflare.com/profile/api-tokens
DRY_RUN=1 bash scripts/cf_allow_programmatic_ua_public_api.sh   # inspect first
bash scripts/cf_allow_programmatic_ua_public_api.sh
```

This touches a **WAF custom rule**, not the zone Worker. Deploying `worker.js`
remains a manual dashboard paste (an API PUT drops its bindings); nothing here
goes near it.

### Why this scoping and not "just allow the UA"

The skip requires **method AND path AND UA** to all match. It cannot be used to
POST, it does not apply to admin or write paths, and it is not a global UA
allowance. It skips only `bic` and `uaBlock` — the two products that key off the
UA signature — so **WAF managed rules, rate limiting, hotlink protection and
security level all stay active** on this traffic. (The 2026-06-06 self-probe
rule skips all seven products; do not copy that breadth here.)

### Step 3 — verify from the outside

Not from the dashboard, and not from the API response:

```bash
python3 scripts/check_public_api_programmatic_access.py --base https://dchub.cloud
```

It sends urllib's genuine default UA, cache-busts every request, prints
`cf-cache-status` per path, and exits non-zero on any 403.

---

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
