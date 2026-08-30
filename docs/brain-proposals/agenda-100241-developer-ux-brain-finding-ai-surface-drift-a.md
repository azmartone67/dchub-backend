<!-- fingerprint:36e636cf9df0f07b3f3996b80eb97f84 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [developer_ux] Brain finding: ai_surface_drift:agents_md:pro_period @ https://dchub.cloud/AGENTS.md (seen

> Auto-captured from an **approved** brain agenda item (#100241). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-30T21:02:24.973676Z · agenda #100241_

## The approved recommendation

Approve (or reject) a structural fix: build a single canonical plan/tier-limit config that generates AGENTS.md, mcp.json, server-card.json, and llms.txt data blocks at build/deploy time, with a CI gate that blocks deploys on canon divergence — versus continuing the current detect-then-patch loop via the 2h sentinel cron. If approved, also decide whether the sentinel's 'expected' values should be sourced from that same canon (one source) or kept independent (two-source cross-check).

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
