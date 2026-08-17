<!-- fingerprint:3369c1425d024a881ee72e43b55286cc -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: cron_silently_dead @ /api/jobs/gas-refresh (seen x4508210)

> Auto-captured from an **approved** brain agenda item (#100131). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-23T06:19:44.910681Z · agenda #100131_

## The approved recommendation

Decide (a) whether gas-refresh (and ai-wars/site-baseline) should be revived with a declared interval or retired into _INTENTIONAL_STALE_CRONS, and (b) whether to approve the systemic work: a deploy-time cron registry fence, per-(cron,condition) finding deduplication in the detector, and per-job heartbeat alerting — versus continuing per-instance revivals.

## Rolled-up targets — class `cron_silently_dead` (class collapse, 2026-08-17)

This doc is now the single obligation for **10 occurrences** of
`cron_silently_dead`. The other 9 were closed against it. They are listed here
in full so the collapse loses no target — fixing the class means fixing
every line below, and a fix that only covers this doc's own target has not
discharged the obligation.

- `/api/jobs/gas-refresh` — was `agenda-100131-reliability-brain-finding-cron-silently-dead.md` (filed 2026-07-23)
- `/api/jobs/site-baseline` — was `agenda-100132-reliability-brain-finding-cron-silently-dead.md` (filed 2026-07-23)
- `/api/jobs/global-intelligence` — was `agenda-100159-reliability-brain-finding-cron-silently-dead.md` (filed 2026-08-02)
- `/api/jobs/energy-discovery` — was `agenda-100166-reliability-brain-finding-cron-silently-dead.md` (filed 2026-08-04)
- `/api/jobs/ai-outreach` — was `agenda-100167-reliability-brain-finding-cron-silently-dead.md` (filed 2026-08-04)
- `/api/jobs/ai-ecosystem` — was `agenda-100168-reliability-brain-finding-cron-silently-dead.md` (filed 2026-08-04)
- `/api/jobs/site-baseline` — was `agenda-100171-reliability-brain-finding-cron-silently-dead.md` (filed 2026-08-05)
- `/api/jobs/gas-refresh` — was `agenda-100173-reliability-brain-finding-cron-silently-dead.md` (filed 2026-08-05)
- `/api/jobs/global-intelligence` — was `agenda-100176-reliability-brain-finding-cron-silently-dead.md` (filed 2026-08-06)
- `/api/jobs/gas-refresh` — was `inv-100064-cron-silently-dead-observed-at-api-jobs-gas-r.md` (filed 2026-08-10)

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
