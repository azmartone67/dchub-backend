# Brain proposal — [data_coverage] 4922 verified vs 21957 tracked facilities (17035 in the unverified discovery pile)

> Auto-captured from an **approved** brain prop item (#5). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-11T18:55:15.136297Z · prop #5_

## The approved recommendation

Approve the sequencing: (1) direct engineering to repair/reconcile the verified-flag attribution in discovered_facilities (4,922 canonical vs 0 in breakdowns) before any prioritization is trusted; then (2) choose between the US/PJM geography-first sprint (2,163 facilities, telemetry-backed) or the known-operator batch (Digital Realty/Equinix/AWS, ~1,852 facilities, lowest verification friction) as the first conversion wave — or authorize both in parallel if verification capacity allows.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
