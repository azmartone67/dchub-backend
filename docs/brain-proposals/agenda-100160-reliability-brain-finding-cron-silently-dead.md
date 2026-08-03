<!-- fingerprint:4d5f7a264cb42a59b5324619c480e3ec -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: cron_silently_dead @ /api/jobs/market-report (value 156,843)

> Auto-captured from an **approved** brain agenda item (#100160). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-03T19:53:37.046320Z · agenda #100160_

## The approved recommendation

Decide whether market-report is a live job to REVIVE (re-enable the scheduler entry, rotate/fix the auth credential behind the 401, and de-conflict the '7 */6 * * *' collision slot) or a retired job to REGISTER in _INTENTIONAL_STALE_CRONS. Only one of those two actions moves the 156843s counter into a healthy range; everything else re-detects the same condition.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
