<!-- fingerprint:714a824243b38d293f0ff2fa08cd2c01 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: cron_silently_dead @ /api/jobs/ai-outreach (value 287,115)

> Auto-captured from an **approved** brain agenda item (#100167). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-04T17:57:07.128526Z · agenda #100167_

## The approved recommendation

Decide (1) whether ai-outreach is still wanted — revive it or add it to _INTENTIONAL_STALE_CRONS; and (2) whether to authorize a scheduler-level intervention (fix the auth secret causing 401s / restart the scheduler container) covering all five dead crons at once, versus continuing per-job fixes that evidence suggests have not stuck.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
