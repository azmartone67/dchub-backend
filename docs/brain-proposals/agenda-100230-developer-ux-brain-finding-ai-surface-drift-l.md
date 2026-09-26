<!-- fingerprint:921589e372c40bde44a4d684af41ab70 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [developer_ux] Brain finding: ai_surface_drift:llms_txt:stale_value @ https://dchub.cloud/llms.txt (seen

> Auto-captured from an **approved** brain agenda item (#100230). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-26T09:29:06.269948Z · agenda #100230_

## The approved recommendation

Approve building a single canon-driven templating/regeneration pipeline for ALL AI surfaces (llms.txt, llms-full.txt, AGENTS.md, mcp.json, server-card.json, chatgpt/grok configs) sourced from canonical_stats and the live tools registry — versus continuing per-instance self-heal patches — and decide whether it runs at serve time, build time, or via the existing self-heal lane.

## Triage — 2026-09-26 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md`, which stays
OPEN as the single obligation for `ai_surface_drift`. This doc's target —
`llms_txt:stale_value @ https://dchub.cloud/llms.txt (seen` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-09-26 as a class member of prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md (class collapse)