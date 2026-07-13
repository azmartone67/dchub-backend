# Brain proposal — [data_coverage] 4922 verified vs 21957 tracked facilities (17035 in the unverified discovery pile)

> Auto-captured from an **approved** brain prop item (#24). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-13T05:25:30.053065Z · prop #24_

## The approved recommendation

Choose the verification sequencing: (a) first fix/reconcile the verified-flag pipeline (0-verified breakdown vs 4,922 canonical) before any campaign, (b) launch provider-batch verification against Digital Realty/Equinix/AWS lists (~1,850 facilities) now, or (c) launch the US/PJM geographic push (2,163 facilities, telemetry-cross-checkable) now — and whether to accept the risk of running (b)/(c) before (a) is resolved.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
