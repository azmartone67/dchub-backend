<!-- fingerprint:c4b444a9f0d54394e53a3a8dd3d66ebe -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: cron_silently_dead @ /api/jobs/global-intelligence (value 485,604)

> Auto-captured from an **approved** brain agenda item (#100176). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-06T18:20:16.798098Z · agenda #100176_

## The approved recommendation

Choose one: (1) revive the job — fix the scheduler's auth so calls to /api/jobs/global-intelligence return 200, and restart/verify the shared scheduler container that appears to have killed four crons simultaneously; or (2) formally retire it by adding global-intelligence to _INTENTIONAL_STALE_CRONS. Also decide whether to treat this as a single shared-scheduler incident covering energy-discovery, gas-refresh, and content-publish rather than four separate tickets.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
