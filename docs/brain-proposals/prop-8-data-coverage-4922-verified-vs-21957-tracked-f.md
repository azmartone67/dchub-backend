# Brain proposal — [data_coverage] 4922 verified vs 21957 tracked facilities (17035 in the unverified discovery pile)

> Auto-captured from an **approved** brain prop item (#8). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-11T18:55:07.697920Z · prop #8_

## The approved recommendation

Choose the sequencing: (A) first spend an engineering pass reconciling the verified-flag definition (0 vs 4,922 conflict) before allocating any verification labor, or (B) start verification immediately on the US/PJM cluster (2,163 facilities) accepting the risk that some are already verified under the canonical definition, or (C) prioritize the provider-normalization cleanup (~2,855 unattributed facilities) to unlock operator-list batch verification across all regions. A then B is the investigator's suggested order.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
