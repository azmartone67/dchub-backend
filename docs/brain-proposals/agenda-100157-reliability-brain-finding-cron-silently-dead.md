<!-- fingerprint:57c56a6111f013da6e07d2c73d020797 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: cron_silently_dead @ /api/jobs/content-publish (seen x117710)

> Auto-captured from an **approved** brain agenda item (#100157). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-02T03:06:55.512110Z · agenda #100157_

## The approved recommendation

Approve the two-part structural fix: (a) add fingerprint dedup / open-state tracking to the cron_silently_dead detector so it stops emitting per-pass duplicates, and (b) build a generic heartbeat/dead-man-switch for all cron jobs with a scheduler-auth verification pass (checking whether PR #2105's gating broke the scheduler's calls) — versus continuing per-instance remediation under the existing #1506 approach.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
