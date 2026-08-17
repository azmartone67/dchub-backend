<!-- fingerprint:d44add71b2ffb13876c9fb28fd4592dc -->
# Brain proposal — [reliability] Brain finding: frontend_endpoint_slow @ /dashboard (value 5,941)

> Auto-captured from an **approved** brain agenda item (#100100). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-15T07:48:00.285613Z · agenda #100100_

## The approved recommendation

Decide whether to authorize the two remediation tracks — (a) clear/redeploy the stuck Cloudflare Pages build and (b) refactor the 5 unsafe DB connection patterns in routes/deals_routes.py — and confirm the healthy latency threshold from HEALTH_BASELINE.md that the /dashboard metric (currently 5941) must stay under, across multiple detector passes, before this finding is marked resolved.

## Triage — 2026-08-17 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/agenda-49-reliability-brain-finding-frontend-endpoint-s.md`, which stays
OPEN as the single obligation for `frontend_endpoint_slow`. This doc's target —
`/dashboard` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on agenda-49-reliability-brain-finding-frontend-endpoint-s.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is agenda-49-reliability-brain-finding-frontend-endpoint-s.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in agenda-49-reliability-brain-finding-frontend-endpoint-s.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against agenda-49-reliability-brain-finding-frontend-endpoint-s.md
- [x] Or close this PR if superseded / not worth it — closed 2026-08-17 as a class member of agenda-49-reliability-brain-finding-frontend-endpoint-s.md (class collapse)