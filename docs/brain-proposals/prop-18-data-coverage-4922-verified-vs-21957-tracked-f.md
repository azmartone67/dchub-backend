# Brain proposal — [data_coverage] 4922 verified vs 21957 tracked facilities (17035 in the unverified discovery pile)

> Auto-captured from an **approved** brain prop item (#18). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-13T05:25:41.900815Z · prop #18_

## The approved recommendation

Choose the sequencing: (1) authorize a data-integrity fix first (reconcile the 4,922-verified canonical figure against the 0-verified breakdowns, and clear the 91 aborted-transaction sync errors) before committing verification resources, versus (2) proceed immediately with the two conversion plays — US/PJM cluster (2,163 facilities) and operator-batch verification of Digital Realty/Equinix/AWS (~1,850 facilities, after provider normalization) — accepting that the regional gap denominators may shift once the flag defect is fixed.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
