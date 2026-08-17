<!-- fingerprint:c4b444a9f0d54394e53a3a8dd3d66ebe -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: cron_silently_dead @ /api/jobs/global-intelligence (value 485,604)

> Auto-captured from an **approved** brain agenda item (#100176). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-06T18:20:16.798098Z · agenda #100176_

## The approved recommendation

Choose one: (1) revive the job — fix the scheduler's auth so calls to /api/jobs/global-intelligence return 200, and restart/verify the shared scheduler container that appears to have killed four crons simultaneously; or (2) formally retire it by adding global-intelligence to _INTENTIONAL_STALE_CRONS. Also decide whether to treat this as a single shared-scheduler incident covering energy-discovery, gas-refresh, and content-publish rather than four separate tickets.

## Triage — 2026-08-17 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/agenda-100131-reliability-brain-finding-cron-silently-dead.md`, which stays
OPEN as the single obligation for `cron_silently_dead`. This doc's target —
`/api/jobs/global-intelligence` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on agenda-100131-reliability-brain-finding-cron-silently-dead.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is agenda-100131-reliability-brain-finding-cron-silently-dead.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in agenda-100131-reliability-brain-finding-cron-silently-dead.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against agenda-100131-reliability-brain-finding-cron-silently-dead.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-17 as a class member of agenda-100131-reliability-brain-finding-cron-silently-dead.md (class collapse)