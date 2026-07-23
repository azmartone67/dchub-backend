<!-- fingerprint:3369c1425d024a881ee72e43b55286cc -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: cron_silently_dead @ /api/jobs/gas-refresh (seen x4508210)

> Auto-captured from an **approved** brain agenda item (#100131). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-23T06:19:44.910681Z · agenda #100131_

## The approved recommendation

Decide (a) whether gas-refresh (and ai-wars/site-baseline) should be revived with a declared interval or retired into _INTENTIONAL_STALE_CRONS, and (b) whether to approve the systemic work: a deploy-time cron registry fence, per-(cron,condition) finding deduplication in the detector, and per-job heartbeat alerting — versus continuing per-instance revivals.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
