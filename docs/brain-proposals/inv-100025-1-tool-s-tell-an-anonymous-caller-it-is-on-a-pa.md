<!-- fingerprint:ae4b97b478f443c466c26ceb11899172 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — 1 tool(s) tell an ANONYMOUS caller it is on a paid tier — observed from the anon seat on mcp: get_energy_prices -> caller_tier='pro' — no API key was sent; 3 tool(s) called; all tier fields seen: ['get_energy_prices:caller_tier=pro'] What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100025). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-06T03:04:42.461018Z · inv #100025_

## The approved recommendation

Choose one: (a) accept that PR #136 already fixed this and just schedule an anon-seat re-verification probe of get_energy_prices, or (b) treat the observation as post-merge and open a regression ticket against dchub-mcp-server #136 requiring the tier-resolution path for get_energy_prices to be audited before any new fix is written.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
