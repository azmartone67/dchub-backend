# Brain proposal — [data_coverage] 4922 verified vs 21957 tracked facilities (17035 in the unverified discovery pile)

> Auto-captured from an **approved** brain prop item (#100029). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-13T21:53:09.984642Z · prop #100029_

## The approved recommendation

Choose the sequencing: (a) pause geographic conversion work until the verified-flag inconsistency (0 vs 4,922) is diagnosed and the per-region breakdown is trustworthy, or (b) proceed now with the US/PJM cluster + operator-route (Digital Realty/Equinix/AWS) verification push accepting the risk that the targeting data is partially broken. Also decide whether provider-name normalization of the ~2,855 unknown-provider records is in scope for this effort.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
