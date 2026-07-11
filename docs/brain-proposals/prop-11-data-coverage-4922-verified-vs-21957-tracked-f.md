# Brain proposal — [data_coverage] 4922 verified vs 21957 tracked facilities (17035 in the unverified discovery pile)

> Auto-captured from an **approved** brain prop item (#11). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-11T18:54:54.578743Z · prop #11_

## The approved recommendation

Choose the sequencing: (A) fix the verification-status join in the segment breakdowns first so per-country/provider/ISO verification rates are actually measurable, then re-prioritize; or (B) proceed now on the volume-based bet — a US/PJM verification sprint plus a provider-normalization pass on the ~2,855 unattributed facilities — accepting the risk that the 4,922 already-verified facilities may overlap heavily with that same segment.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
