<!-- fingerprint:df906d5db87a24b6112058441da12c89 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [developer_ux] Brain finding: iso_metric_count_zero_24h @ grid_data: iso=AECI (seen x1)

> Auto-captured from an **approved** brain agenda item (#100248). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-05T06:22:47.130380Z · agenda #100248_

## The approved recommendation

Open routes/iso_orchestrator.py, map every fan-out ISO code (AECI, CPLE, DUK, TVA, SC, GVL, JEA, FPL) to its dispatch, and implement a single shared last-write-per-ISO-code freshness check at the grid_data write path that emits an alert when any code's last write exceeds its expected cadence — before the 24h zero-count detector fires.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
