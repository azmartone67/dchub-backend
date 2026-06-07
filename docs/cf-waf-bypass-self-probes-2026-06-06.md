# CF WAF allowlist for DC Hub self-probes (stop DDoSing ourselves)

**Date filed:** 2026-06-06
**Owner:** infra
**Status:** PARTIAL — existing UA-only rule (id `5f9a1d837e9c4608a1a52efc174d7677`) from 2026-05-20 is too narrow; this doc upgrades it.

---

## Problem

Cross-referencing the user's CF analytics logs:

- `162.220.232.99` is the egress IP for DC Hub's own internal probes (Railway worker
  pool) — UA strings include `DCHub-FailoverProbe/1.0`, `DCHub-RenderFlapCheck/1.0`,
  `DCHub-BrainRadar/1.0`, `DCHub-BrainUniformity/1.0`, `DCHub-RedirCheck/1.0`,
  `DCHubHealer/1.0`, etc.
- That same IP is the **#1 entry in Cloudflare "Top Mitigated IPs" with 1.53M blocked
  requests in 7 days** — out of 2.64M total mitigated, so **~58% of all WAF blocks
  are our own self-probes** (~218K requests/day).
- The CF WAF treats high-volume probing as an attack and is blocking via WAF Managed
  Rules + Rate Limiting + Bot Fight Mode (BIC).

## Why the existing rule isn't enough

There IS already a rule in the `http_request_firewall_custom` ruleset:

```
rule_id: 5f9a1d837e9c4608a1a52efc174d7677
desc:    Allow DC Hub internal probes (FF+25-followup, 2026-05-20)
expr:    (lower(http.user_agent) contains "dchub")
phases:  [http_request_firewall_managed, http_request_sbfm]
products: [rateLimit, uaBlock, bic]
```

But it has four gaps that let ~218K/day through:

1. **Expression matches UA only** — probes whose UA is blank, generic
   (`python-requests/*`, `curl/*`), or hasn't been updated to the `DCHub-` convention
   are not covered.
2. **Phases missing `http_ratelimit`** — rate-limit rules can still trigger.
3. **Products missing `waf`, `hot`, `zoneLockdown`, `securityLevel`** — core WAF +
   security level still trigger.
4. **No `ruleset: "current"`** — doesn't skip the entire managed ruleset, only the
   wrapping phase.

Compare the (working) MCP allowlist rule which has all four:

```
rule_id: 0158a074af0640a69a72a25882a3e3dd
expr:    (starts_with(http.request.uri.path, "/mcp"))
phases:  [http_ratelimit, http_request_firewall_managed, http_request_sbfm]
products: [bic, uaBlock, hot, zoneLockdown, securityLevel, rateLimit, waf]
ruleset: current
```

## The fix

Upgrade rule `5f9a1d837e9c4608a1a52efc174d7677` to match the MCP rule's coverage,
broaden the expression to include IP + UA prefix variants, and bump the description.

**Target rule state:**

```json
{
  "action": "skip",
  "action_parameters": {
    "phases": [
      "http_ratelimit",
      "http_request_firewall_managed",
      "http_request_sbfm"
    ],
    "products": [
      "bic",
      "hot",
      "rateLimit",
      "securityLevel",
      "uaBlock",
      "waf",
      "zoneLockdown"
    ],
    "ruleset": "current"
  },
  "description": "DC Hub internal probes - self-DDoS bypass (2026-06-06)",
  "enabled": true,
  "expression": "(ip.src eq 162.220.232.99) or (starts_with(http.user_agent, \"DCHub-\")) or (starts_with(http.user_agent, \"dchub-\")) or (http.user_agent contains \"DCHubHealer\") or (lower(http.user_agent) contains \"dchub\")",
  "logging": { "enabled": true }
}
```

---

## Why this wasn't applied automatically

The Railway env `CLOUDFLARE_API_TOKEN` and the local `~/.wrangler` OAuth token both
have **zone-read scope only** (`zone:read`). Neither can PATCH/PUT the rulesets API:

```
$ curl -X PATCH ... rulesets/.../rules/5f9a1d837e9c4608a1a52efc174d7677
{"errors":[{"code":10405,"message":"Method not allowed for this authentication scheme"}]}
```

To apply the fix programmatically, generate a new token at
<https://dash.cloudflare.com/profile/api-tokens> with:

- **Zone** > **Zone WAF** > **Edit** (covers Custom Rules / rulesets)
- Zone Resources: `Include` > `Specific zone` > `dchub.cloud`

Then either:

### Option A: Run the helper script (preferred)

```bash
export CF_WAF_EDIT_TOKEN="cf_token_with_zone_waf_edit_scope"
bash /Users/jonathanmartone/dchub-backend/scripts/cf_waf_allowlist_self_probes.sh
```

The script PATCHes the existing rule and verifies the result. It is idempotent —
safe to re-run.

### Option B: Apply via CF dashboard (UI)

1. Sign in to <https://dash.cloudflare.com>.
2. Pick the `dchub.cloud` zone.
3. **Security** > **WAF** > **Custom rules**.
4. Edit the existing rule **"Allow DC Hub internal probes (FF+25-followup,
   2026-05-20"** (the rule will show id `5f9a1d8...` in the URL).
5. Replace the **Expression** with (raw editor):

   ```
   (ip.src eq 162.220.232.99) or (starts_with(http.user_agent, "DCHub-")) or (starts_with(http.user_agent, "dchub-")) or (http.user_agent contains "DCHubHealer") or (lower(http.user_agent) contains "dchub")
   ```

6. **Action**: **Skip**.
7. **Skip the following ruleset phases**: tick **Bot Fight Mode** (`http_request_sbfm`), **Managed Rules** (`http_request_firewall_managed`), **Rate limiting rules** (`http_ratelimit`).
8. **Skip the following products**: tick **all** — `Bot Intelligence Check`, `Hotlink protection`, `Rate limiting (previous version)`, `Security level`, `User Agent block`, `Web Application Firewall (managed rules)`, `Zone Lockdown`.
9. **Description**: `DC Hub internal probes - self-DDoS bypass (2026-06-06)`.
10. **Save**.

### Option C: Apply via legacy Firewall Rules API

Deprecated by Cloudflare but still works:

```bash
curl -X POST \
  -H "Authorization: Bearer $CF_WAF_EDIT_TOKEN" \
  -H "Content-Type: application/json" \
  https://api.cloudflare.com/client/v4/zones/1cb22dda8d50546d6edf0c09a8be5128/firewall/rules \
  -d '[{
    "action": "bypass",
    "products": ["waf","rateLimit","bic","uaBlock","hot","securityLevel","zoneLockdown"],
    "description": "DC Hub internal probes - self-DDoS bypass (2026-06-06)",
    "filter": {
      "expression": "(ip.src eq 162.220.232.99) or (starts_with(http.user_agent, \"DCHub-\")) or (starts_with(http.user_agent, \"dchub-\")) or (http.user_agent contains \"DCHubHealer\") or (lower(http.user_agent) contains \"dchub\")"
    }
  }]'
```

---

## Verification

After applying, wait ~30s for CF edge propagation, then:

```bash
# Should return 200 (used to return 403/429 for prod IP):
curl -sI "https://dchub.cloud/api/v1/version" \
     -H "User-Agent: DCHub-FailoverProbe/1.0" \
     | head -3

# In ~5 min, check CF "Top Mitigated IPs" - 162.220.232.99 should drop off
# the dashboard or fall dramatically.
```

## Rollback

To remove if it causes problems (e.g., a real attacker spoofs `User-Agent: DCHub-X`):

```bash
curl -X DELETE \
  -H "Authorization: Bearer $CF_WAF_EDIT_TOKEN" \
  https://api.cloudflare.com/client/v4/zones/1cb22dda8d50546d6edf0c09a8be5128/rulesets/phases/http_request_firewall_custom/entrypoint/rules/5f9a1d837e9c4608a1a52efc174d7677
```

(or revert via dashboard).

**Risk note:** the `ip.src eq 162.220.232.99` clause is a hard allow on that specific
IP. If Railway ever changes the egress IP, we'll silently lose the bypass on legacy
probes (the UA-prefix clauses still catch the named probes). If a hostile actor ever
controls that IP, they'd bypass WAF entirely — Railway IP rotation is the mitigation.
Verify the egress IP periodically with:

```bash
curl -s https://dchub.cloud/api/v1/admin/whoami-egress 2>/dev/null | jq .egress_ip
# or: railway run --service dchub-backend python -c "import urllib.request; print(urllib.request.urlopen('https://ifconfig.me').read())"
```

## References

- Existing rule id: `5f9a1d837e9c4608a1a52efc174d7677`
- Entrypoint ruleset id: `eb3d5e52de5e42d789ace8f174939a26`
- Phase: `http_request_firewall_custom`
- Zone id: `1cb22dda8d50546d6edf0c09a8be5128` (dchub.cloud)
- Account id: `4bb33ec40ef02f9f4b41dc97668d5a52`
- Mirror rule for comparison: MCP allowlist `0158a074af0640a69a72a25882a3e3dd`
