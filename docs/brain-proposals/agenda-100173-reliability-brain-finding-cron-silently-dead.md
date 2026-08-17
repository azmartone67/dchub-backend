<!-- fingerprint:0c549493c01f783a36e0f5e9e611b346 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: cron_silently_dead @ /api/jobs/gas-refresh (value 304,956)

> Auto-captured from an **approved** brain agenda item (#100173). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-05T21:41:20.129588Z · agenda #100173_

## The approved recommendation

Choose one: (a) fix the scheduler invocation of /api/jobs/gas-refresh (correct HTTP method + keyed request) and verify a 200-status run, which resets the 304956s counter; or (b) declare gas-refresh retired and add it to _INTENTIONAL_STALE_CRONS so the detector stops firing. Also decide whether to audit news-refresh (417782s) under the same hypothesis in the same pass.

## Triage — 2026-08-17 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/agenda-100131-reliability-brain-finding-cron-silently-dead.md`, which stays
OPEN as the single obligation for `cron_silently_dead`. This doc's target —
`/api/jobs/gas-refresh` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on agenda-100131-reliability-brain-finding-cron-silently-dead.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is agenda-100131-reliability-brain-finding-cron-silently-dead.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in agenda-100131-reliability-brain-finding-cron-silently-dead.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against agenda-100131-reliability-brain-finding-cron-silently-dead.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-17 as a class member of agenda-100131-reliability-brain-finding-cron-silently-dead.md (class collapse)