<!-- fingerprint:75edf577bdb0657a46031ac3aca101ce -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: cron_silently_dead @ /api/jobs/ai-ecosystem (value 307,136)

> Auto-captured from an **approved** brain agenda item (#100168). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-04T17:57:00.765208Z · agenda #100168_

## The approved recommendation

Choose the target end-state for the ai-ecosystem cron: (a) revive it — repair the scheduler credential causing the 401s, verify a scheduled call 200s AND records a last-run (which is what actually resets the 307136 counter), and apply the same fix to ai-outreach; or (b) retire it — add ai-ecosystem to _INTENTIONAL_STALE_CRONS so the detector stops firing. Option (a) vs (b) depends on whether this job's output is still wanted, which only the operator knows.

## Triage — 2026-08-17 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/agenda-100131-reliability-brain-finding-cron-silently-dead.md`, which stays
OPEN as the single obligation for `cron_silently_dead`. This doc's target —
`/api/jobs/ai-ecosystem` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on agenda-100131-reliability-brain-finding-cron-silently-dead.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is agenda-100131-reliability-brain-finding-cron-silently-dead.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in agenda-100131-reliability-brain-finding-cron-silently-dead.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against agenda-100131-reliability-brain-finding-cron-silently-dead.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-17 as a class member of agenda-100131-reliability-brain-finding-cron-silently-dead.md (class collapse)