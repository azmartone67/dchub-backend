# Brain proposal — [data_coverage] 4924 verified vs 21959 tracked facilities (17035 in the unverified discovery pile)

> Auto-captured from an **approved** brain agenda item (#100103). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-16T07:04:34.183650Z · agenda #100103_

## The approved recommendation

Choose the sequencing: (1) block verification spend until the verified-flag discrepancy (4,924 canonical vs 0 in all breakdowns) is diagnosed, or proceed in parallel; then (2) pick the first conversion wave — US/PJM geographic batch (2,163 facilities, leveraging existing ISO telemetry) vs operator crosswalk batch (Digital Realty + Equinix + AWS, ~1,852 facilities) — or approve both and defer the 'Unknown'-provider tail (2,855 facilities) explicitly.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
