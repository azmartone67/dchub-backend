<!-- fingerprint:714a824243b38d293f0ff2fa08cd2c01 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: cron_silently_dead @ /api/jobs/ai-outreach (value 287,115)

> Auto-captured from an **approved** brain agenda item (#100167). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-04T17:57:07.128526Z · agenda #100167_

## The approved recommendation

Decide (1) whether ai-outreach is still wanted — revive it or add it to _INTENTIONAL_STALE_CRONS; and (2) whether to authorize a scheduler-level intervention (fix the auth secret causing 401s / restart the scheduler container) covering all five dead crons at once, versus continuing per-job fixes that evidence suggests have not stuck.

## Triage — 2026-08-17 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/agenda-100131-reliability-brain-finding-cron-silently-dead.md`, which stays
OPEN as the single obligation for `cron_silently_dead`. This doc's target —
`/api/jobs/ai-outreach` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on agenda-100131-reliability-brain-finding-cron-silently-dead.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is agenda-100131-reliability-brain-finding-cron-silently-dead.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in agenda-100131-reliability-brain-finding-cron-silently-dead.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against agenda-100131-reliability-brain-finding-cron-silently-dead.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-17 as a class member of agenda-100131-reliability-brain-finding-cron-silently-dead.md (class collapse)