<!-- fingerprint:3a5e7de1d8a30f108180bd1c284565fb -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — agent-eval: openai's #1 structural ask

> Auto-captured from an **approved** brain agenda item (#100120). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-20T10:04:46.356940Z · agenda #100120_

## The approved recommendation

Approve (or reject) making the contract registry the single source of truth: one generated artifact chain (registry → OpenAPI → MCP/handoff manifests → runtime envelope validators) with a drift-failing CI gate hooked into the model-relations eval loop — versus continuing with targeted per-endpoint patches to the site-score spec and analyze_site handoff. Also decide whether /api/site-score's response must be migrated to the PROVENANCE ENVELOPE v1 pattern as part of this change or as a follow-up.

## Triage — 2026-08-17 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/agenda-100116-agent-eval-cohere-s-1-structural-ask.md`, which stays
OPEN as the single obligation for `structural_ask`. This doc's target —
`openai` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on agenda-100116-agent-eval-cohere-s-1-structural-ask.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is agenda-100116-agent-eval-cohere-s-1-structural-ask.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in agenda-100116-agent-eval-cohere-s-1-structural-ask.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against agenda-100116-agent-eval-cohere-s-1-structural-ask.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-17 as a class member of agenda-100116-agent-eval-cohere-s-1-structural-ask.md (class collapse)