<!-- fingerprint:db2d61c23f280d421e155821bb5b6704 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [developer_ux] Brain finding: ai_surface_drift:llms_full:stale_value @ https://dchub.cloud/llms-full.txt

> Auto-captured from an **approved** brain prop item (#100067). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-25T04:09:33.155599Z · prop #100067_

## The approved recommendation

Approve building a canonical-stats-driven render pipeline for ALL AI surfaces with a deploy-time diff gate (one-time engineering investment that retires the ai_surface_drift finding class), versus continuing to auto-patch each stale surface as detectors flag it. If approved, also decide whether the detector semantics change from value-diffing to pipeline-freshness verification.

## Rolled-up targets — class `ai_surface_drift` (class collapse, 2026-08-17)

This doc is now the single obligation for **5 occurrences** of
`ai_surface_drift`. The other 4 were closed against it. They are listed here
in full so the collapse loses no target — fixing the class means fixing
every line below, and a fix that only covers this doc's own target has not
discharged the obligation.

- `https://dchub.cloud/llms-full.txt` — was `prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md` (filed 2026-07-25)
- `https://dchub.cloud/connect` — was `agenda-100181-developer-ux-brain-finding-ai-surface-drift-c.md` (filed 2026-08-09)
- `https://dchub.cloud/.well-known/mcp` — was `agenda-100182-developer-ux-brain-finding-ai-surface-drift-m.md` (filed 2026-08-09)
- `https://dchub.cloud/.well-known/` — was `prop-100112-developer-ux-brain-finding-ai-surface-drift-m.md` (filed 2026-08-09)
- `https://dchub.cloud/.well-known/mcp.` — was `agenda-100199-developer-ux-brain-finding-ai-surface-drift-m.md` (filed 2026-08-16)

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
