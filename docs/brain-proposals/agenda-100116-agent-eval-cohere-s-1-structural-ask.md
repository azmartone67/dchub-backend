<!-- fingerprint:ba741c57b2a723784b0affd4ccf207f4 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — agent-eval: cohere's #1 structural ask

> Auto-captured from an **approved** brain agenda item (#100116). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-19T12:11:04.764508Z · agenda #100116_

## The approved recommendation

Approve a platform-wide shared GeoJSON geometry serializer (opt-in geometry=full parameter, representative_point default) built on the analyze_parcel Phase 3 geometry layer — versus a one-off patch to the interconnection queue endpoint only — and decide whether to first audit whether full geometries exist upstream for interconnection queue records before committing to the contract change.

## Rolled-up targets — class `structural_ask` (class collapse, 2026-08-17)

This doc is now the single obligation for **6 occurrences** of
`structural_ask`. The other 5 were closed against it. They are listed here
in full so the collapse loses no target — fixing the class means fixing
every line below, and a fix that only covers this doc's own target has not
discharged the obligation.

- `cohere` — was `agenda-100116-agent-eval-cohere-s-1-structural-ask.md` (filed 2026-07-19)
- `meta` — was `agenda-100118-agent-eval-meta-s-1-structural-ask.md` (filed 2026-07-19)
- `mistral` — was `agenda-100119-agent-eval-mistral-s-1-structural-ask.md` (filed 2026-07-20)
- `openai` — was `agenda-100120-agent-eval-openai-s-1-structural-ask.md` (filed 2026-07-20)
- `perplexity` — was `agenda-100121-agent-eval-perplexity-s-1-structural-ask.md` (filed 2026-07-20)
- `xai` — was `agenda-100122-agent-eval-xai-s-1-structural-ask.md` (filed 2026-07-20)

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
