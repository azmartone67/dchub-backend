<!-- fingerprint:9e8b1166f03d0a9399757520a558a1c0 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: cron_silently_dead @ /api/jobs/site-baseline (seen x1020337)

> Auto-captured from an **approved** brain agenda item (#100132). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-23T06:19:41.436174Z · agenda #100132_

## The approved recommendation

Three choices: (1) For site-baseline specifically — revive the schedule or retire it into _INTENTIONAL_STALE_CRONS? (2) Approve building the deploy-time cron registry validation (schedule-or-retire enforced in CI)? (3) Approve the finding-dedup change (one open finding per condition with last_seen), after confirming it isn't already covered by the 2026-07-17 finding_false_closed_refired fix?

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
