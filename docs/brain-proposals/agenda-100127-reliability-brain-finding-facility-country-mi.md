<!-- fingerprint:e2ea6225c659125a3fed50f2ed62ccfd -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: facility_country_mislabeled @ /api/v1/admin/facility-geo/analyze (seen x105

> Auto-captured from an **approved** brain agenda item (#100127). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-22T03:25:19.993922Z · agenda #100127_

## The approved recommendation

Approve two things: (1) execute the one-time reversible batch correction (POST /api/v1/admin/facility-geo/apply?confirm=1) for the current 105 mislabeled facilities, and (2) authorize engineering work to move the coordinate-vs-country check from the post-hoc analyze detector into the facility write/ingestion path (auto-correct high-confidence, quarantine ambiguous) with a HEALTH_BASELINE fence — or decide instead to keep the current detect-and-patch loop if the write-path change is judged too invasive.

## Rolled-up targets — class `facility_country_mislabeled` (class collapse, 2026-08-17)

This doc is now the single obligation for **2 occurrences** of
`facility_country_mislabeled`. The other 1 were closed against it. They are listed here
in full so the collapse loses no target — fixing the class means fixing
every line below, and a fix that only covers this doc's own target has not
discharged the obligation.

- `/api/v1/admin/facility-geo/analyze` — was `agenda-100127-reliability-brain-finding-facility-country-mi.md` (filed 2026-07-22)
- `/api/v1/admin/facility-geo/analyze` — was `inv-100066-facility-country-mislabeled-observed-at-api-v.md` (filed 2026-08-10)

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
