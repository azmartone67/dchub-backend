<!-- fingerprint:4d5f7a264cb42a59b5324619c480e3ec -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: cron_silently_dead @ /api/jobs/market-report (value 124,322)

> Auto-captured from an **approved** brain agenda item (#100232). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-27T18:47:40.313170Z · agenda #100232_

## The approved recommendation

Choose one of three paths: (1) inspect the scheduler's auth credential for /api/jobs/market-report and rotate/fix it so calls return 200 (recommended, given the 401×7 pattern); (2) if the job is retired, add market-report to _INTENTIONAL_STALE_CRONS to silence re-detection; or (3) if the 401s turn out to be external noise and the scheduler truly never fires, re-register the cron entry and confirm a 200 run. Also decide whether the detector should distinguish 'called but 401' from 'never called' so this class self-diagnoses next time.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
