<!-- fingerprint:f26d025aa4086ae67cbc8c65aae10730 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: cron_silently_dead @ /api/jobs/market-report (seen x126960)

> Auto-captured from an **approved** brain agenda item (#100158). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-02T03:06:51.967155Z · agenda #100158_

## The approved recommendation

Approve which fix ships first: (a) detector-side fingerprint dedup + escalation so one dead cron produces one escalating finding (kills the 126k-finding flood platform-wide, also covers content-publish's 130,558), or (b) job-side repair — restore the scheduler's authenticated access post-PR #2105 and add a last-success heartbeat check for /api/jobs/market-report. Recommended: both, with (a) prioritized since it addresses the recurrence mechanism itself; but confirm whether the shipped finding_false_closed_refired fix already claims coverage of this class before funding new detector work.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
