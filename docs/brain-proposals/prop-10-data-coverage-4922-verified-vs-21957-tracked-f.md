# Brain proposal — [data_coverage] 4922 verified vs 21957 tracked facilities (17035 in the unverified discovery pile)

> Auto-captured from an **approved** brain prop item (#10). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-11T18:55:00.218815Z · prop #10_

## The approved recommendation

Decide the sequencing: (A) pause geographic prioritization and first fix/reconcile the verified-flag pipeline (is_duplicate/merged_at semantics vs canonical_stats), or (B) proceed now with the US/PJM cluster (2,163 facilities) as the first verification batch and the unknown-provider normalization pass (2,855 facilities) as the parallel workstream, accepting the risk that some are already verified under the canonical definition.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
