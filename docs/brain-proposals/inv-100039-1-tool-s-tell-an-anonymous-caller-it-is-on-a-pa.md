<!-- fingerprint:ae4b97b478f443c466c26ceb11899172 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — 1 tool(s) tell an ANONYMOUS caller it is on a paid tier — observed from the anon seat on mcp: get_energy_prices -> caller_tier='pro' — no API key was sent; 3 tool(s) called; all tier fields seen: ['get_energy_prices:caller_tier=pro'] What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100039). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-07T15:15:35.682567Z · inv #100039_

## The approved recommendation

Choose: (A) accept that PR #2329 closed this and schedule only a verification probe (anon call to get_energy_prices asserting caller_tier != 'pro'), or (B) treat #2329 as insufficient and authorize a code audit of the shared tier-resolution/fallback path to enforce fail-closed 'anonymous' tier for un-keyed callers across all 82 tools. Also decide whether to add a permanent regression fence (automated anon-seat probe asserting no paid tier is ever returned without a validated key).

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
