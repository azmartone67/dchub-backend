# Brain proposal — [reliability] Brain finding: data_freshness_sla_breach @ table:usgs_water_stress (seen x2847)

> Auto-captured from an **approved** brain agenda item (#100099). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-15T07:48:06.762403Z · agenda #100099_

## The approved recommendation

Approve the two-layer structural fix: (a) repair/restart the usgs_water_stress cron after checking Railway logs to confirm failure mode, and (b) authorize engineering work on cron dead-man's-switch monitoring plus fingerprint-based finding deduplication for all recurring detector types — or choose to only patch the single cron and accept the class recurring elsewhere.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
