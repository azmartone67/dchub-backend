<!-- fingerprint:39ed1e7d3f7c687abbcbd1532da36a86 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [developer_ux] Brain finding: ai_surface_drift:chatgpt_instructions:stale_value @ https://dchub.cloud/int

> Auto-captured from an **approved** brain agenda item (#100255). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-08T17:24:08.943597Z · agenda #100255_

## The approved recommendation

In ai_surface_sentinel.py, trace the 'update-from-canon' path (line 158) to its canon source and refactor the chatgpt_instructions() endpoint in main.py:26336 to render instructions.txt from that same canon dict at serve-time, then extend the pattern to the mcp_json/server_card/agents_md surfaces so the *_period and *_tier fields are generated rather than stored.

## Triage — 2026-09-26 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md`, which stays
OPEN as the single obligation for `ai_surface_drift`. This doc's target —
`chatgpt_instructions:stale_value @ https://dchub.cloud/int` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-09-26 as a class member of prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md (class collapse)