# Brain proposal — [reliability] Brain finding: dedup_pipeline_stalled @ /api/v1/facilities/delta (seen x21422)

> Auto-captured from an **approved** brain agenda item (#61). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-05T21:07:32.380490Z · agenda #61_

## The approved recommendation

Approve (a) refactoring the dedup_pipeline_stalled detector from per-record findings to a single backlog-SLO finding, and (b) authorizing a provider-name normalization + prioritized batch dedup run against the 21,422-record backlog — or, alternatively, direct an engineer to first pull the detector source and pipeline runtime metrics to confirm the per-record fan-out hypothesis before any refactor.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
