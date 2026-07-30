<!-- fingerprint:e2ea6225c659125a3fed50f2ed62ccfd -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: facility_country_mislabeled @ /api/v1/admin/facility-geo/analyze (seen x136

> Auto-captured from an **approved** brain agenda item (#100149). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-30T02:55:02.953230Z · agenda #100149_

## The approved recommendation

Two approvals: (1) Run the existing reversible backlog fix now (POST /api/v1/admin/facility-geo/apply?confirm=1) to correct the 136 current mislabels — yes/no. (2) Authorize engineering work to move the facility-geo analyze check into the ingestion write path as a blocking validator, choosing between auto-derive country from coordinates vs. quarantine-and-review on mismatch.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
