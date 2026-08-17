<!-- fingerprint:13d0a66b2993629f6871bb0173e653bd -->
# Brain proposal — [reliability] Brain finding: frontend_endpoint_slow @ / (value 5,840)

> Auto-captured from an **approved** brain agenda item (#100097). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-14T09:09:04.885763Z · agenda #100097_

## The approved recommendation

Decide whether to (a) prioritize unblocking the Cloudflare Pages deploy pipeline (the 10× cf_pages_deploy_stuck signal) as the presumed root cause for the ~5.8s-class latency on '/' and '/dashboard', or (b) first instrument the frontend_endpoint_slow detector to expose the metric's unit and healthy threshold (per HEALTH_BASELINE.md) before committing engineering effort — and confirm whether 5840 is milliseconds or something else.

## Triage — 2026-08-17 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/agenda-49-reliability-brain-finding-frontend-endpoint-s.md`, which stays
OPEN as the single obligation for `frontend_endpoint_slow`. This doc's target —
`/` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on agenda-49-reliability-brain-finding-frontend-endpoint-s.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is agenda-49-reliability-brain-finding-frontend-endpoint-s.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in agenda-49-reliability-brain-finding-frontend-endpoint-s.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against agenda-49-reliability-brain-finding-frontend-endpoint-s.md
- [x] Or close this PR if superseded / not worth it — closed 2026-08-17 as a class member of agenda-49-reliability-brain-finding-frontend-endpoint-s.md (class collapse)