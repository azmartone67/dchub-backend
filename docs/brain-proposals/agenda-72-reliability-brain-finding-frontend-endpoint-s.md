<!-- fingerprint:609572940ddb105e54ccff9a448033bd -->
# Brain proposal — [reliability] Brain finding: frontend_endpoint_slow @ /construction-pipeline (value 6,508)

> Auto-captured from an **approved** brain agenda item (#72). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-09T06:28:20.579073Z · agenda #72_

## The approved recommendation

Choose the remediation path: (1) prioritize clearing the 21,911-item dedup backlog so pipeline pages query the small verified set, (2) ship a cached/precomputed aggregate layer for /construction-pipeline, /capacity-pipeline, and /system-status without waiting on dedup, or (3) first run a profiling trace to confirm the bottleneck before committing engineering effort. Also decide whether the stuck Cloudflare Pages deploys (×10) must be unblocked first.

## Triage — 2026-08-17 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/agenda-49-reliability-brain-finding-frontend-endpoint-s.md`, which stays
OPEN as the single obligation for `frontend_endpoint_slow`. This doc's target —
`/construction-pipeline` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on agenda-49-reliability-brain-finding-frontend-endpoint-s.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is agenda-49-reliability-brain-finding-frontend-endpoint-s.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in agenda-49-reliability-brain-finding-frontend-endpoint-s.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against agenda-49-reliability-brain-finding-frontend-endpoint-s.md
- [x] Or close this PR if superseded / not worth it — closed 2026-08-17 as a class member of agenda-49-reliability-brain-finding-frontend-endpoint-s.md (class collapse)