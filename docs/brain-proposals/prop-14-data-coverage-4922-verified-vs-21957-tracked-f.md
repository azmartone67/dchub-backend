# Brain proposal — [data_coverage] 4922 verified vs 21957 tracked facilities (17035 in the unverified discovery pile)

> Auto-captured from an **approved** brain prop item (#14). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-11T18:55:18.033345Z · prop #14_

## The approved recommendation

Two-part decision: (1) Authorize a data-integrity fix first — reconcile why per-country/per-ISO queries show 0 verified while canonical_stats shows 4,922 verified — before spending verification effort; and (2) approve or reject the proposed prioritization of US/PJM (2,163 tracked facilities) as the first verification batch, with DE and FR as the next tranches, versus an alternative such as clearing the 2,855 Unknown-provider records first.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
