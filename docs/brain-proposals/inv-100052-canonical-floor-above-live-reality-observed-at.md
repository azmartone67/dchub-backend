<!-- fingerprint:3a337f4618d84855085fb93ba0d48cfe -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — canonical_floor_above_live_reality (observed at: ai_surface_canon.PINNED.public). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it? If there is no mechanical fix, say so plainly and explain why.

> Auto-captured from an **approved** brain inv item (#100052). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-09T07:52:39.438748Z · inv #100052_

## The approved recommendation

Choose between: (1) verify PR #2463 fully covers PINNED.public.facilities (plus stale_markers) and close this finding as already-fixed, or (2) fund the structural fix — derive PINNED floors from get_canonical_stats() at publish time with round-down semantics, and add a fence asserting floor <= live verified — accepting that hand-edited floors will otherwise drift again.

## Triage — 2026-08-17 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/agenda-77-reliability-brain-finding-canonical-floor-abo.md`, which stays
OPEN as the single obligation for `canonical_floor_above_live_reality`. This doc's target —
`ai_surface_canon.PINNED.public` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on agenda-77-reliability-brain-finding-canonical-floor-abo.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is agenda-77-reliability-brain-finding-canonical-floor-abo.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in agenda-77-reliability-brain-finding-canonical-floor-abo.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against agenda-77-reliability-brain-finding-canonical-floor-abo.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-17 as a class member of agenda-77-reliability-brain-finding-canonical-floor-abo.md (class collapse)