# Brain proposal — [data_coverage] 4922 verified vs 21957 tracked facilities (17035 in the unverified discovery pile)

> Auto-captured from an **approved** brain prop item (#9). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-11T18:55:04.563565Z · prop #9_

## The approved recommendation

Choose the sequencing: (A) pause geographic targeting and first repair/backfill the verification-flag propagation in discovered_facilities (recommended, since all slice-level verified counts read 0), or (B) proceed now on volume-ranked targets — US/PJM cluster plus Digital Realty/Equinix/AWS operator batch verification — accepting that the location-level gap data may be misranked until the flag is fixed. Also decide whether to fund the provider-normalization pass needed to unlock the ~2,855 unknown-provider facilities.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
