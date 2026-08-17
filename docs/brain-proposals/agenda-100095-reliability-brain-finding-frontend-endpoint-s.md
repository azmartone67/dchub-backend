<!-- fingerprint:c13c776ba9e443a12f8f826e20c3e658 -->
# Brain proposal — [reliability] Brain finding: frontend_endpoint_slow @ /ai-pipeline (value 6,775)

> Auto-captured from an **approved** brain agenda item (#100095). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-14T07:27:05.224123Z · agenda #100095_

## The approved recommendation

Approve (a) profiling the shared request path behind /ai-pipeline and /construction-pipeline to confirm the common bottleneck, and (b) implementing a precomputed/cached aggregation for that path — and explicitly confirm the frontend_endpoint_slow healthy threshold (and unit) from HEALTH_BASELINE.md/detector config so remediation is judged by the value re-entering range, not by the alert going quiet.

## Triage — 2026-08-17 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/agenda-49-reliability-brain-finding-frontend-endpoint-s.md`, which stays
OPEN as the single obligation for `frontend_endpoint_slow`. This doc's target —
`/ai-pipeline` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on agenda-49-reliability-brain-finding-frontend-endpoint-s.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is agenda-49-reliability-brain-finding-frontend-endpoint-s.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in agenda-49-reliability-brain-finding-frontend-endpoint-s.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against agenda-49-reliability-brain-finding-frontend-endpoint-s.md
- [x] Or close this PR if superseded / not worth it — closed 2026-08-17 as a class member of agenda-49-reliability-brain-finding-frontend-endpoint-s.md (class collapse)