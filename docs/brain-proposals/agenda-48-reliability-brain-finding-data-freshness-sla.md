# Brain proposal — [reliability] Brain finding: data_freshness_sla_breach @ table:usgs_water_stress (seen x2547)

> Auto-captured from an **approved** brain agenda item (#48). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-02T23:29:31.088504Z · agenda #48_

## The approved recommendation

Choose the remediation path: (1) implement stateful open/close breach findings for freshness (and likely all high-count detectors) as the class-level fix, (2) first pull the usgs_water_stress SLA config, ingestion job logs, and breach timestamps to confirm whether this is chronic staleness vs. repeated pipeline failures before changing alerting, or (3) both in parallel. Also decide whether the freshness SLA should be recalibrated to USGS's actual publication cadence.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
