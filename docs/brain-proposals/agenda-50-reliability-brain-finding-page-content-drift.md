<!-- fingerprint:5b71e777832a3da359c3418a6f4915d1 -->
# Brain proposal — [reliability] Brain finding: page_content_drift:/admin/funnel-health @ /admin/funnel-health (seen x288)

> Auto-captured from an **approved** brain agenda item (#50). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-03T09:40:51.400991Z · agenda #50_

## The approved recommendation

Approve (a) moving /admin/* dashboard routes from content-hash drift to structural-diff monitoring with volatile-region exclusions, and (b) adding signature-based dedup/aggregation to the Brain findings pipeline — or decide instead to simply mute this one route, accepting that the same noise pattern (cf. 2,559 freshness-breach findings) will keep recurring elsewhere.

## Rolled-up targets — class `page_content_drift` (class collapse, 2026-08-17)

This doc is now the single obligation for **3 occurrences** of
`page_content_drift`. The other 2 were closed against it. They are listed here
in full so the collapse loses no target — fixing the class means fixing
every line below, and a fix that only covers this doc's own target has not
discharged the obligation.

- `/admin/funnel-health` — was `agenda-50-reliability-brain-finding-page-content-drift.md` (filed 2026-07-03)
- `/api/v1/brain/heartbeat` — was `agenda-58-reliability-brain-finding-page-content-drift.md` (filed 2026-07-04)
- `/api/v1/admin/` — was `agenda-66-reliability-brain-finding-page-content-drift.md` (filed 2026-07-07)

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
