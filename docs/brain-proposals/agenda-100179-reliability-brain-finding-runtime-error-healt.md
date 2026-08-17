<!-- fingerprint:e7f2d20fa56b38cd4c584c7741fe0188 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: runtime_error:Health check failed (<n>/<n>): self_response @ dchub://runtim

> Auto-captured from an **approved** brain agenda item (#100179). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-07T15:15:34.014538Z · agenda #100179_

## The approved recommendation

Choose between (a) approving the instrumentation-first + hysteresis approach (diagnose on next occurrence, suppress transient noise), (b) going straight to a code-level fix of the brain_findings writer locking on the contention hypothesis without confirmation, or (c) first auditing whether the 4 occurrences predate the 2026-07-03 fix and closing the finding as already-resolved. Also decide whether transient WARN-level health failures should file brain findings at all.

## Triage — 2026-08-17 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/prop-100041-reliability-brain-finding-runtime-error-sync.md`, which stays
OPEN as the single obligation for `runtime_error`. This doc's target —
`dchub://runtim` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on prop-100041-reliability-brain-finding-runtime-error-sync.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is prop-100041-reliability-brain-finding-runtime-error-sync.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in prop-100041-reliability-brain-finding-runtime-error-sync.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against prop-100041-reliability-brain-finding-runtime-error-sync.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-17 as a class member of prop-100041-reliability-brain-finding-runtime-error-sync.md (class collapse)