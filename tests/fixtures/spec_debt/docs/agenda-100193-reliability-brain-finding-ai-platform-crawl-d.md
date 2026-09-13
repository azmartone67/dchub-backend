<!-- fingerprint:6c7eba35257ef9d3a3466aa112488a92 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: ai_platform_crawl_drop:meta @ ai_requests (seen x2244)

> Auto-captured from an **approved** brain agenda item (#100193). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-13T18:09:46.339051Z · agenda #100193_

## The approved recommendation

Decide whether the priority is (A) making the detector stateful/auto-closing so it stops emitting 2244 duplicate findings, or (B) investigating and repairing the actual Meta crawl surface (robots/llms.txt/sitemap/WAF) — and confirm the current magnitude of the Meta crawl drop before committing engineering time to either.

## Triage — 2026-08-17 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/agenda-74-reliability-brain-finding-ai-platform-crawl-d.md`, which stays
OPEN as the single obligation for `ai_platform_crawl_drop`. This doc's target —
`ai_requests` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on agenda-74-reliability-brain-finding-ai-platform-crawl-d.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is agenda-74-reliability-brain-finding-ai-platform-crawl-d.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in agenda-74-reliability-brain-finding-ai-platform-crawl-d.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against agenda-74-reliability-brain-finding-ai-platform-crawl-d.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-17 as a class member of agenda-74-reliability-brain-finding-ai-platform-crawl-d.md (class collapse)