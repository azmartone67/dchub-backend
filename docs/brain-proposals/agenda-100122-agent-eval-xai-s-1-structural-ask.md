<!-- fingerprint:6817cf7f5e20ebaacf34c95885b420c6 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — agent-eval: xai's #1 structural ask

> Auto-captured from an **approved** brain agenda item (#100122). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-20T21:07:15.150482Z · agenda #100122_

## The approved recommendation

Approve (or reject) replacing the hand-maintained OpenAPI spec with a generated-from-route-registry contract plus a deploy-blocking CI drift fence — accepting the one-time migration cost and stricter deploy gate — versus continuing to patch individual envelope/path mismatches (site-score, queue_results, ranked_sites, analyze_site) as they're flagged.

## Triage — 2026-08-17 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/agenda-100116-agent-eval-cohere-s-1-structural-ask.md`, which stays
OPEN as the single obligation for `structural_ask`. This doc's target —
`xai` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on agenda-100116-agent-eval-cohere-s-1-structural-ask.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is agenda-100116-agent-eval-cohere-s-1-structural-ask.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in agenda-100116-agent-eval-cohere-s-1-structural-ask.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against agenda-100116-agent-eval-cohere-s-1-structural-ask.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-17 as a class member of agenda-100116-agent-eval-cohere-s-1-structural-ask.md (class collapse)