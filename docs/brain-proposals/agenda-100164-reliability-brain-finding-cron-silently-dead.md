<!-- fingerprint:787fdbc87df6fdc9816fe49794e8d95e -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: cron_silently_dead @ /api/jobs/content-publish (value 250,192)

> Auto-captured from an **approved** brain agenda item (#100164). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-03T19:53:32.273236Z · agenda #100164_

## The approved recommendation

Decide: (a) revive content-publish by repairing the scheduler entry/dispatch path (and confirm the 401s were the scheduler failing auth), (b) retire it and add it to _INTENTIONAL_STALE_CRONS to silence the detector, or (c) hold if PR #2147 already restored dispatch — verify a fresh successful run first. Also decide whether to treat all three stale crons as one scheduler-level incident.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
