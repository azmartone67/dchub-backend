# MCP Server Integration: Close the Paywall → Click → Conversion Funnel

**Status:** Backend ready (Phase FF+7, 2026-05-19). MCP server change required to land.

## The problem

`/api/v1/redeem/funnel-stats` (30-day window):

| Stage | Count |
|---|---|
| paywall_hit | 15,436 |
| **click** | **1** |
| view | 1 |
| submit | 0 |
| upgrade | 6 (from non-paywall channels) |

**0.0065% click rate.** Brain L14 identified this as the actual conversion-crisis root cause: the MCP paywall response sends agents to a bare `https://dchub.cloud/pricing` URL with no pair-code, so users have no 1-click path back to the tool that paywalled them.

## What the backend now provides

Two new endpoints (live as of commit `3a16156a`):

### Option A (preferred): single-call paywall payload

```
POST https://dchub.cloud/api/v1/mcp/paywall-response
X-API-Key: <caller's api_key>
Content-Type: application/json

{
  "tool":   "get_grid_intelligence",
  "market": "chicago",             // optional
  "agent":  "claude-desktop",      // optional (referring AI client name)
  "reason": "Paid tier required"   // optional, agent-facing
}
```

Response:

```json
{
  "ok": true,
  "reason": "Paid tier required for full result set",
  "pair_code": "DCM-M23G",
  "redeem_url": "https://dchub.cloud/redeem/DCM-M23G",
  "upgrade_url": "https://dchub.cloud/upgrade?key=...&tool=...&market=...",
  "status_poll_url": "https://dchub.cloud/api/v1/mcp/pair-code/DCM-M23G/status",
  "expires_at": "2026-05-19T08:05:31+00:00",
  "message_to_agent": "This tool requires the Developer tier. Tell the human to visit https://dchub.cloud/redeem/DCM-M23G to upgrade (one click, 30-min code). I'll poll status and unlock as soon as they complete checkout.",
  "reused": false
}
```

This call:
- Mints a pair-code (idempotent: same caller within 30min gets the same code)
- Records a `paywall_hit` row in `mcp_upgrade_signals` (so funnel attribution is correct)
- Returns everything the MCP server needs

### Option B (drop-in URL replacement)

If a code change in the MCP server is too heavy, just replace the literal `upgrade_url` value:

| Current | New |
|---|---|
| `https://dchub.cloud/pricing` | `https://dchub.cloud/upgrade?key=<api_key>&tool=<tool>` |

`/upgrade` is a smart-redirect: it mints a pair-code and 302s to `/redeem/<code>`. No backend call needed from the MCP server — the user's browser does the 302 dance.

## Suggested integration code (Node, server.mjs)

```javascript
// When a paywall is hit on a paid-only tool:
async function emitPaywall(apiKey, toolName, market, agentName) {
  try {
    const r = await fetch("https://dchub.cloud/api/v1/mcp/paywall-response", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-API-Key": apiKey,
      },
      body: JSON.stringify({
        tool: toolName,
        market: market || null,
        agent: agentName || null,
      }),
    });
    if (r.ok) {
      const payload = await r.json();
      // Ship this back to the agent as a structured tool response
      return {
        error: "paid_only",
        reason: payload.reason,
        upgrade_url: payload.redeem_url,           // direct deep-link
        fallback_url: payload.upgrade_url,         // /upgrade?... entry-point
        message: payload.message_to_agent,
        pair_code: payload.pair_code,
        status_poll_url: payload.status_poll_url,
        expires_at: payload.expires_at,
      };
    }
  } catch (_) { /* fall through to legacy */ }

  // Legacy fallback (current behavior)
  return {
    error: "paid_only",
    reason: "Paid tier required",
    upgrade_url: "https://dchub.cloud/pricing",  // ← the leak L14 found
  };
}
```

## Verification after deploy

1. POST `https://dchub.cloud/api/v1/mcp/paywall-response` with a real api_key — confirm 200 with full payload
2. Watch `/api/v1/redeem/funnel-stats` over 24h — `click` count should start climbing
3. Brain detector `check_paywall_click_leak` will auto-resolve when click rate ≥ 0.5% (currently 0.0065%)

## Why this matters

- 15,436 paywall hits in 30 days at the current 1:5586 conversion ratio = 2.7 expected conversions/year
- At a target 1:100 ratio = 154 expected conversions/year — 57× lift, no additional traffic
- Estimated MRR uplift at $49/mo Developer: ~$7,400/mo at full lift, captured purely by fixing this URL

## Out of band

If the MCP server team can't deploy quickly, the legacy `/pricing` URL will keep working — the conversion gap just stays open. Don't roll back the backend changes; `/upgrade` is purely additive.
