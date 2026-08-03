<!-- fingerprint:ea3ee089281202c63725abe8a84e365a -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: cron_silently_dead @ /api/jobs/news-refresh (value 219,836)

> Auto-captured from an **approved** brain agenda item (#100165). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-03T19:53:19.858074Z · agenda #100165_

## The approved recommendation

Decide whether news-refresh (and content-publish, ai-ecosystem) should be REVIVED — fix the 401 auth/secret and scheduler entry so a 200 run resets the staleness clock — or formally RETIRED via _INTENTIONAL_STALE_CRONS. Also decide whether to treat the three dead crons as one shared-scheduler incident or three separate tickets.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
