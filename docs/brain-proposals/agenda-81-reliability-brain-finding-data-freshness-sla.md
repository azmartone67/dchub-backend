# Brain proposal — [reliability] Brain finding: data_freshness_sla_breach @ table:usgs_water_stress (seen x2736)

> Auto-captured from an **approved** brain agenda item (#81). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-10T19:37:56.278090Z · agenda #81_

## The approved recommendation

Approve the two-part fix: (a) authorize investigation of Railway logs and re-registration/hardening of the usgs_water_stress cron with retry + missed-window heartbeat, and (b) approve changing detector semantics to one deduplicated finding per breach episode (which will collapse the 2,736-entry backlog) — or choose to keep per-cycle findings and only fix the cron.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
