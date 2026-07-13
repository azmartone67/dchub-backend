# Brain proposal — [data_coverage] 4922 verified vs 21957 tracked facilities (17035 in the unverified discovery pile)

> Auto-captured from an **approved** brain prop item (#28). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-13T05:25:13.546232Z · prop #28_

## The approved recommendation

Choose the sequencing: (1) pause segment-targeted verification until the verified-flag discrepancy (0 vs 4,922) is reconciled, or (2) proceed now on the volume-ranked targets — US/PJM cluster and Digital Realty/Equinix/AWS provider batches — accepting that per-segment verification rates are currently unmeasurable. Also decide whether the ~2,855 unknown-provider records get a dedup/triage track or are included in the verification queue.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
