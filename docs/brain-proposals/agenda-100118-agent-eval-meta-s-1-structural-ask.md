<!-- fingerprint:0954d9ad6ba6e9b1d66a35ff0e96b0c6 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — agent-eval: meta's #1 structural ask

> Auto-captured from an **approved** brain agenda item (#100118). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-19T23:33:15.616870Z · agenda #100118_

## The approved recommendation

Approve building a unified search_facilities endpoint (REST + MCP tool) as the next platform capability — and decide three scoping choices: (1) search over verified-only (4,966) or all tracked (22,002) with a verification flag, (2) whether search is free-tier bait that funnels into paid grid/fiber tools or itself metered, and (3) whether to gate the launch on a provider-name normalization pass given the dirty operator field.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
