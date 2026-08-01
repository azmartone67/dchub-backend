<!-- fingerprint:9e8b1166f03d0a9399757520a558a1c0 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: cron_silently_dead @ /api/jobs/site-baseline (seen x477455)

> Auto-captured from an **approved** brain agenda item (#100156). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-01T20:00:20.810214Z · agenda #100156_

## The approved recommendation

Approve the two-part fix: (1) implement finding fingerprint deduplication + resolution tracking in the brain findings pipeline (one open finding per condition, occurrence counter), and (2) audit the scheduler's invocation of /api/jobs/site-baseline for the 405 method mismatch and add a per-job last-success heartbeat — versus continuing per-instance remediation. Also decide whether to bulk-collapse the existing 477,455 duplicate finding rows once dedup lands.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
