<!-- fingerprint:cb5b3c78b949a265fcd2d713459f10a3 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [data_coverage] 320 DCPI markets across 7 live ISOs

> Auto-captured from an **approved** brain agenda item (#100198). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-16T09:50:20.568198Z · agenda #100198_

## The approved recommendation

Choose the coverage investment sequence: (a) depth-first — obtain PJM_API_KEY and ISO-NE web-services credentials to bring the remaining 2 of 7 ISOs fully live (recommended), (b) breadth-first — add live telemetry to new ISOs/international markets beyond the current 7, or (c) split effort. Also decide whether to first commission the missing cost/timeline and customer-demand measurements before committing.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
