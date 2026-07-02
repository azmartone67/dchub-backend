# Brain proposal — [reliability] Brain finding: dedup_pipeline_stalled @ /api/v1/facilities/delta (seen x21422)

> Auto-captured from an **approved** brain agenda item (#46). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-02T06:05:17.124715Z · agenda #46_

## The approved recommendation

Approve (a) prioritizing a dedup pipeline overhaul — key normalization (provider/operator canonicalization) plus checkpointed batch draining of the 21,422-record backlog — over per-instance auto-remediation, and (b) changing the dedup_pipeline_stalled detector from per-record findings to a single backlog-depth/age SLO finding. Also decide the target SLO (e.g., acceptable unverified backlog size and max record age) that defines 'fixed'.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
