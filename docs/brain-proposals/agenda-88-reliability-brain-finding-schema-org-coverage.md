# Brain proposal — [reliability] Brain finding: schema_org_coverage_low @ /api/v1/schema-org/missing (seen x76)

> Auto-captured from an **approved** brain agenda item (#88). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-13T05:25:47.130832Z · agenda #88_

## The approved recommendation

Choose the systemic fix path: (1) invest engineering time to move JSON-LD generation into the render pipeline with a CI validation gate (permanent, higher upfront cost), (2) first fix only the remediation loop (idempotent job + rate-limit backoff + detector rollup) and re-assess (cheaper, may still recur on new pages), or (3) do both in sequence. Also decide whether to pull the raw /api/v1/schema-org/missing worklist for a root-cause pass before committing.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
