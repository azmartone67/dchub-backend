<!-- fingerprint:921589e372c40bde44a4d684af41ab70 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [developer_ux] Brain finding: ai_surface_drift:llms_txt:stale_value @ https://dchub.cloud/llms.txt (seen

> Auto-captured from an **approved** brain agenda item (#100230). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-26T09:29:06.269948Z · agenda #100230_

## The approved recommendation

Approve building a single canon-driven templating/regeneration pipeline for ALL AI surfaces (llms.txt, llms-full.txt, AGENTS.md, mcp.json, server-card.json, chatgpt/grok configs) sourced from canonical_stats and the live tools registry — versus continuing per-instance self-heal patches — and decide whether it runs at serve time, build time, or via the existing self-heal lane.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
