<!-- fingerprint:2654da4bdfdc68694d8616bfab966e2d -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [developer_ux] Brain finding: mirror_penalty_actionable_findings_count @ /api/v1/brain/mirror/report (see

> Auto-captured from an **approved** brain agenda item (#100180). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-08T17:28:25.545903Z · agenda #100180_

## The approved recommendation

Choose whether to (a) reclassify mirror_penalty_actionable_findings_count as a dashboard health metric rather than an auto-actionable finding, and (b) approve two pieces of work: root-cause grouping in the mirror's actionable count (extending the existing stateful detector layer) and a single-source-of-truth regenerator for the AI surfaces (mcp.json, AGENTS.md, server-card.json, /connect) to eliminate the ai_surface_drift finding family at its source — versus continuing to patch each drift finding individually.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
