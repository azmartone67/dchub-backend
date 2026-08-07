<!-- fingerprint:ae4b97b478f443c466c26ceb11899172 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — 1 tool(s) tell an ANONYMOUS caller it is on a paid tier — observed from the anon seat on mcp: get_energy_prices -> caller_tier='pro' — no API key was sent; 3 tool(s) called; all tier fields seen: ['get_energy_prices:caller_tier=pro'] What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100037). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-07T07:45:32.411008Z · inv #100037_

## The approved recommendation

Choose the fix scope: (a) minimal patch — fail-closed caller_tier='anonymous' when no valid key, at the single tier-resolution chokepoint, plus one contract-healer invariant; or (b) that patch plus a full sweep of all 82 live tools for tool-tier→caller-tier conflation. Also decide severity: if repro shows real pro data was served to anon callers (not just a mislabel), this becomes an entitlement leak requiring immediate hotfix rather than a metrics fix.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
