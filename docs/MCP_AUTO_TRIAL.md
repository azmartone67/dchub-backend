# MCP Auto-Trial Keys — Agent Integration Guide

**Phase ZZ-3 (2026-05-17)** · For agent runtime authors (Cursor, Cline, Continue, custom Claude agents).

DC Hub's REST + MCP gates use a "soft paywall" pattern that auto-mints
a working trial key on every gated request. **No human signup step
required** — the key is in the response itself. Your agent just needs to
grab it and retry.

## TL;DR

When DC Hub returns HTTP 402 on a REST call:

```
HTTP/2 402
x-trial-key: dch_trial_BRnK3SfUDKQzu66v3ok4xDiKw7TrcQ7z
x-trial-key-expires: 2026-06-16T07:31:50Z
retry-after: 0
www-authenticate: X-API-Key realm="dchub.cloud", claim="https://dchub.cloud/api/v1/keys/claim", tier="DEVELOPER"
link: <https://dchub.cloud/api/v1/keys/auto-trial/redeem>; rel="api-key-redemption"; type="application/json"
```

**Action:** save `X-Trial-Key`, retry the same request with header
`X-API-Key: <value>`. The retry succeeds with full data.

200 calls/day for 30 days, no email required.

## Why this exists

Most agents treat HTTP 402 as a hard error and surface it to the user.
That kills conversion. DC Hub embeds the trial key in **standard HTTP
headers** (RFC 7235 `WWW-Authenticate`, RFC 8288 `Link`, `Retry-After`)
so standard HTTP middleware (httpx, requests-toolbelt, axios
interceptors, OkHttp) can grab the key without parsing the body.

## Minimal Python httpx retry middleware

```python
import httpx

class DCHubTrialAuth(httpx.Auth):
    """Auto-grab trial keys from 402 responses and retry."""
    def __init__(self):
        self.api_key = None

    def auth_flow(self, request):
        if self.api_key:
            request.headers["X-API-Key"] = self.api_key
        response = yield request
        if response.status_code == 402:
            trial_key = response.headers.get("x-trial-key")
            if trial_key:
                self.api_key = trial_key
                request.headers["X-API-Key"] = trial_key
                yield request  # retry with the key

client = httpx.Client(auth=DCHubTrialAuth())
resp = client.get("https://dchub.cloud/api/v1/transactions/export.csv")
# Auto-retries with trial key on 402; second request returns the CSV.
```

## Minimal MCP-side retry (for MCP clients)

The MCP gatekeeper returns gated tool responses as JSON-RPC results.
The trial key is in the `auto_trial_key` field of the result body:

```json
{
  "success": false,
  "error": "upgrade_required",
  "auto_trial_key": "dch_trial_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "auto_trial_expires_at": "2026-06-16T07:31:50Z",
  "auto_trial_daily_calls": 200,
  "message": "✨ Auto-trial key minted ..."
}
```

Pattern: when an MCP tool returns `success: false` + `auto_trial_key`,
your client should:

1. Save the key (treat it like an API key for future tool calls)
2. Re-invoke the same tool with the key in the `X-API-Key` header

Note: MCP `streamable-http` transport requires an `initialize` →
`notifications/initialized` handshake before any tool call. The trial
key gets added to the **outer HTTP request headers** (alongside
`Mcp-Session-Id`), not to the MCP protocol body.

## Soft-paywall responses (gated bulk endpoints)

Anon callers hitting bulk endpoints like `/api/v1/dcpi/scores` get a
preview + structured upgrade signal in the **success** (HTTP 200) body:

```json
{
  "scores": [/* top 10 of 285 */],
  "count": 10,
  "_gated": true,
  "_preview_only": true,
  "_total_available": 285,
  "_hidden_count": 275,
  "_required_tier": "IDENTIFIED",
  "_upgrade_cta": "Showing top 10 of 285 ...",
  "_signup_url": "https://dchub.cloud/signup",
  "_pricing_url": "https://dchub.cloud/pricing"
}
```

Agents that pretty-print responses (Claude, Cursor, Cline) should
surface `_upgrade_cta` to the user when present. The data IS valid —
just truncated. Single-record lookups (`/api/v1/dcpi/scores/<slug>`)
stay free for discovery.

## Tier matrix

| Tier | Daily calls | Price | How to upgrade |
|---|---|---|---|
| Anonymous | 25/24h (Phase NN grace) | $0 | n/a |
| FREE | 100 | $0 | `POST /api/v1/keys/claim` with `{client_name}` |
| IDENTIFIED | 200 | $0 | Add `?email=` to the claim call |
| **Auto-trial** | 200 | $0 (30d) | **No action — auto-minted on 402** |
| DEVELOPER | 2,000 | $49/mo | Stripe link in 402 body |
| PRO | 10,000 | $199/mo | Stripe link in 402 body |
| ENTERPRISE | 100,000 | custom | Contact |

## Discovery

The canonical agent-onboarding manifest is at
`https://dchub.cloud/.well-known/ai-agents.json` (Phase JJ).
Includes MCP server URL, OpenAPI spec, sample queries by intent,
auth matrix, and data-freshness SLA.

## What you DON'T need to do

- No browser flow
- No email verification
- No card on file
- No OAuth dance
- No webhook callback

Just send a GET, read the headers on 402, retry with `X-API-Key`. Done.

## Common pitfalls

1. **Treating 402 as fatal** — 402 with `X-Trial-Key` is a "here's your
   key, try again" signal, not an error. Don't surface to the user.
2. **Ignoring headers, parsing only body** — REST + JSON-RPC envelope
   both deliver the key. Headers are easier for middleware.
3. **Stripping `_gated:true` from successful responses** — these are
   valid HTTP 200s with truncated data. Show the user what they
   actually got, plus the `_upgrade_cta` when present.
4. **Re-claiming on every call** — once you have a working key (trial
   or claimed), use it for the session. The brain tracks usage by key.

---

Questions or weird behavior: hit `/api/v1/health` to confirm the
backend is up, then `/api/v1/keys/auto-trial/stats` to see your own
key's usage. If `call_count: 0` and you have a key, your retry
middleware is the most likely culprit.
