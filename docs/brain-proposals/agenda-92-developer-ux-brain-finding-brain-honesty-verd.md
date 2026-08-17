<!-- fingerprint:5f34ec28672ccb47e5ee98b379abb73d -->
# Brain proposal — [developer_ux] Brain finding: brain_honesty_verdict_disagreement @ /api/v1/brain/evolution vs /api/v1/bra

> Auto-captured from an **approved** brain agenda item (#92). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-13T05:25:21.407013Z · agenda #92_

## The approved recommendation

Approve one of: (a) structural refactor — single canonical verdict module/table that both /api/v1/brain/evolution and /api/v1/brain/self-model read, plus an automated equality fence (recommended); (b) lighter-weight option — keep both computations but add only the fence, which detects but does not prevent drift; or (c) first commission a diagnostic pass (pull endpoint logs and field-level diffs from both detections) before committing to the refactor.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
