<!-- fingerprint:4460bde6789ee507458a9c072a65c813 -->
# Brain proposal — [reliability] Brain finding: page_content_drift:/api/v1/brain/heartbeat @ /api/v1/brain/heartbeat (seen

> Auto-captured from an **approved** brain agenda item (#58). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-04T07:09:00.903705Z · agenda #58_

## The approved recommendation

Approve (a) moving /api/v1/brain/heartbeat and the health-endpoint class from content-hash drift monitoring to a schema/normalized-field check, and (b) implementing a global finding fingerprint + dedup/throttle layer in the Brain pipeline — versus the cheaper but non-durable alternative of a one-off suppression rule for this single endpoint. Also decide whether one manual diff of the heartbeat responses is required first to rule out a real regression before suppression goes live.

## Triage — 2026-08-17 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/agenda-50-reliability-brain-finding-page-content-drift.md`, which stays
OPEN as the single obligation for `page_content_drift`. This doc's target —
`/api/v1/brain/heartbeat` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on agenda-50-reliability-brain-finding-page-content-drift.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is agenda-50-reliability-brain-finding-page-content-drift.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in agenda-50-reliability-brain-finding-page-content-drift.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against agenda-50-reliability-brain-finding-page-content-drift.md
- [x] Or close this PR if superseded / not worth it — closed 2026-08-17 as a class member of agenda-50-reliability-brain-finding-page-content-drift.md (class collapse)