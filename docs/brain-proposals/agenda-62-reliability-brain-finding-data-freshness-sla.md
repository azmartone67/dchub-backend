# Brain proposal — [reliability] Brain finding: data_freshness_sla_breach @ table:usgs_water_stress (seen x2622)

> Auto-captured from an **approved** brain agenda item (#62). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-05T21:07:24.927128Z · agenda #62_

## The approved recommendation

Choose the scope of the fix: (A) fleet-wide — implement stateful finding dedup (one escalating finding per unresolved condition) across ALL detectors, which also collapses the 21,422 dedup_pipeline_stalled and 6,616 frontend_endpoint_slow backlogs; (B) table-only — first instrument usgs_water_stress ingestion, verify USGS upstream cadence, and reset its SLA threshold accordingly; or (C) demote — decide usgs_water_stress is not SLA-worthy and drop it to a lower freshness tier. A+B together is the recommended path; also confirm whether water-stress data matters to any paying use case before investing in B.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
