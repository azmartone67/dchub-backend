<!-- fingerprint:263e15638881e2c6a5d8a7ad466ab30c -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: cron_silently_dead @ /api/jobs/site-baseline (value 250,774)

> Auto-captured from an **approved** brain agenda item (#100171). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-05T05:17:03.748694Z · agenda #100171_

## The approved recommendation

Choose one: (1) revive site-baseline — re-enable its Railway scheduler/cron entry, run it once, confirm a 200 and that the last-run timestamp resets the 250774 counter (and do the same for content-publish at 408609); or (2) formally retire it by adding site-baseline to _INTENTIONAL_STALE_CRONS so the detector stops firing. Also decide whether to open a broader scheduler-container health investigation given multiple jobs are silently dead simultaneously.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
