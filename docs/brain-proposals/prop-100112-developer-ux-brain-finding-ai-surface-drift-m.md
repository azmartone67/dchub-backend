<!-- fingerprint:fac2050283bf097a83a30d9560b020db -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [developer_ux] Brain finding: ai_surface_drift:mcp_json:free_tier_anon @ https://dchub.cloud/.well-known/

> Auto-captured from an **approved** brain prop item (#100112). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-09T19:46:36.713086Z · prop #100112_

## The approved recommendation

Approve building a single canonical config + surface-generation pipeline with a blocking pre-deploy contract test for all AI-discovery surfaces (mcp.json, mcp-server.json, server-card.json, AGENTS.md, /connect) — versus continuing to let the detector/auto-fixer patch each drift instance after it ships. Also decide, per drifted field, whether canon or the live surface is the correct value before the first regeneration run.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
