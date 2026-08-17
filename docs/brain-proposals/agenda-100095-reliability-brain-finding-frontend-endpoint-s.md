<!-- fingerprint:c13c776ba9e443a12f8f826e20c3e658 -->
# Brain proposal — [reliability] Brain finding: frontend_endpoint_slow @ /ai-pipeline (value 6,775)

> Auto-captured from an **approved** brain agenda item (#100095). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-14T07:27:05.224123Z · agenda #100095_

## The approved recommendation

Approve (a) profiling the shared request path behind /ai-pipeline and /construction-pipeline to confirm the common bottleneck, and (b) implementing a precomputed/cached aggregation for that path — and explicitly confirm the frontend_endpoint_slow healthy threshold (and unit) from HEALTH_BASELINE.md/detector config so remediation is judged by the value re-entering range, not by the alert going quiet.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
