<!-- fingerprint:e1d60a8ef318d3ee6e331284ad32ce8a -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: cron_silently_dead @ /api/jobs/global-intelligence (seen x125997)

> Auto-captured from an **approved** brain agenda item (#100159). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-02T06:28:58.895437Z · agenda #100159_

## The approved recommendation

Choose between: (A) fund the structural fix — a declared-interval cron registry + single watchdog with heartbeat and non-200/401 alerting, plus fingerprint-dedup upserts in the findings pipeline so one stale cron equals one open finding; or (B) continue per-instance revival of individual crons (the approach from #1506/#1475 that has already failed to stick). Also decide whether the 401s warrant an immediate scheduler-credential audit before any other work.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
