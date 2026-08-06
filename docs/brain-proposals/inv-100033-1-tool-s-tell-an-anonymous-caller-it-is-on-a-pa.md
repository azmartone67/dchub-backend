<!-- fingerprint:ae4b97b478f443c466c26ceb11899172 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — 1 tool(s) tell an ANONYMOUS caller it is on a paid tier — observed from the anon seat on mcp: get_energy_prices -> caller_tier='pro' — no API key was sent; 3 tool(s) called; all tier fields seen: ['get_energy_prices:caller_tier=pro'] What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100033). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-06T22:37:58.357612Z · inv #100033_

## The approved recommendation

Approve (a) an immediate audit call to confirm whether anon 'pro' classification actually unlocks pro-gated data (leak) or is label-only (metrics bug), and (b) the fail-closed one-point fix: unauthenticated/invalid-key callers always resolve to tier 'anon'/'free', with an anon-seat regression probe added across all 82 tools. Decide whether to also re-open the mcp_tool_zero_conversion findings for get_energy_prices/get_renewable_energy pending re-measurement after the fix.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
