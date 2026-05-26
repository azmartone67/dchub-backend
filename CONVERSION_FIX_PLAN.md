# Conversion-fix sequence — what shipped tonight, what to ship in 24h

## Tonight (cf7a5016 — already on `main`, deploying)

### #15 — Tier table normalized everywhere

Two files now state the same ladder:

| Tier | calls/day | price | Stripe |
|---|---|---|---|
| anonymous | 10 | free, no signup | — |
| free (dev key) | 1,000 | free, email signup | dchub.cloud/signup |
| **starter (NEW)** | **10,000** | **$9/mo** | 8x2dRa5sS0x75uteGuaZi0g |
| developer | unlimited paid tools | $49/mo | 7sY5kE8F4fs13mI0PEaZi0c |
| pro | unlimited + Pro tools | $199/mo | — |
| enterprise | unlimited | custom | api@dchub.cloud |

**What changed:** previously the `/api/v1/upgrade-hint` payload told blocked agents that the free tier was 10,000/day while Developer was only 1,000/day — so the upgrade looked like a *downgrade*. Pro was also listed at $499 in some paywall variants ($199 is the real price). Both fixed.

### Files touched
- `routes/mcp_funnel_upgrade.py` — tier table in `/api/v1/upgrade-hint` payload
- `routes/paywall_hint_middleware.py` — A/B/C copy variants in 401/403/429 responses

## In 24 hours (only if #15 hasn't already moved the needle)

### #16 — Tighten the free-preview (single-line worker change)

Right now, unauthenticated callers get **5 free results per tool call** (worker `MCP_TIERS.free.results_limit = 5`). For high-volume tools like `search_facilities` (4,260 calls/7d) and `get_market_intel` (2,527 calls/7d), this is too generous — the agent gets enough data without ever surfacing the paywall to the user.

**Proposed change** in `PATCHES/dchubapiproxy-v4.9.13-VERSION-SYNC.js` line 611:

```diff
- free:       { name: 'Free',       daily_limit: 10,     results_limit: 5,     fields_truncated: true,  export_allowed: false },
+ free:       { name: 'Free',       daily_limit: 10,     results_limit: 2,     fields_truncated: true,  export_allowed: false },
```

Going from 5 → 2 (60% squeeze) is moderate. Two results is enough for "yes, there's data" but not enough to answer most questions, which surfaces the existing CTA banner ("Showing 2 of N results. Free dev key unlocks all N.").

If 2 doesn't move conversion in another 24h, go to 1. Don't drop below 1 (returning zero results triggers the agent's empty-result handling and the user never sees the paywall CTA).

### Verify after #16 ships

```bash
# 24h after #16:
curl -s "https://dchub.cloud/api/v1/mcp/conversion-funnel?days=1" | python3 -c "
import json,sys
d=json.load(sys.stdin)
for f in d['funnels']:
    s = f['stages']
    if s['1_paywall_signals'] > 0:
        print(f.get('tool',''), 'signals:', s['1_paywall_signals'], 'codes:', s['2_codes_minted'])
"
```

Healthy signal: paywall_signals > 0 for the top 3 tools (search_facilities, get_market_intel, get_news) and codes_minted > 0 for at least one of them. Before #15, all tools had 0 paywall_signals (the funnel SQL appears broken — but that's a measurement bug, not a conversion bug, and we can revisit).

## What's still measurement-broken (separate task)

- `tool_calls_7d_real` returns 0 — the probe-filter is too aggressive, filtering out all real traffic
- `/api/v1/mcp/conversion-funnel/timeseries` returns `{"error":"no_data"}`
- `brain-warming/detectors` returns `api_calls_24h: 0` and `mcp_tool_usage_total_7d: 0` while funnel shows 25K calls/7d — detector SQL is broken
- `/citations/recent` endpoint returns 0 — no systematic capture of "Groq cited us"

These are SQL-query bugs, not feature bugs. Worth fixing after the conversion question is settled, so we can actually measure what #15 and #16 do.
