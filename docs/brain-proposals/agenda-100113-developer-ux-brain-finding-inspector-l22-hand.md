<!-- fingerprint:d40b9a767749191ded917c5e6b9e02f7 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [developer_ux] Brain finding: inspector_l22_handoff @ /api/v1/brain/brief/100228/draft-prs (seen x2)

> Auto-captured from an **approved** brain agenda item (#100113). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-18T08:55:26.521680Z · agenda #100113_

## The approved recommendation

Choose between: (A) authorize an investigation-and-fix of the L22 handoff trigger plus a recipe-dedup/escalation registry and handoff-SLA fence (the durable fix), or (B) manually draft/merge the two currently-proposed recipe PRs for brief 100228 as a one-off patch, accepting the finding will recur on future briefs. If (A), also decide whether the handoff should remain human-gated with an explicit approval queue or fire autonomously.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
