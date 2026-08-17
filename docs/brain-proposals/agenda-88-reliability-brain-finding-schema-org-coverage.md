<!-- fingerprint:5950ce2a33a329dfa18b47e0b09353ff -->
# Brain proposal — [reliability] Brain finding: schema_org_coverage_low @ /api/v1/schema-org/missing (seen x76)

> Auto-captured from an **approved** brain agenda item (#88). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-13T05:25:47.130832Z · agenda #88_

## The approved recommendation

Choose the systemic fix path: (1) invest engineering time to move JSON-LD generation into the render pipeline with a CI validation gate (permanent, higher upfront cost), (2) first fix only the remediation loop (idempotent job + rate-limit backoff + detector rollup) and re-assess (cheaper, may still recur on new pages), or (3) do both in sequence. Also decide whether to pull the raw /api/v1/schema-org/missing worklist for a root-cause pass before committing.

## Rolled-up targets — class `schema_org_coverage_low` (class collapse, 2026-08-17)

This doc is now the single obligation for **2 occurrences** of
`schema_org_coverage_low`. The other 1 were closed against it. They are listed here
in full so the collapse loses no target — fixing the class means fixing
every line below, and a fix that only covers this doc's own target has not
discharged the obligation.

- `/api/v1/schema-org/missing` — was `agenda-88-reliability-brain-finding-schema-org-coverage.md` (filed 2026-07-13)
- `/api/v1/schema-org/missing` — was `inv-100090-schema-org-coverage-low-observed-at-api-v1-sc.md` (filed 2026-08-11)

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
