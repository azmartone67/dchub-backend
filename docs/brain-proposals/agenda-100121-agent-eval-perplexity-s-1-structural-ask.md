<!-- fingerprint:95be12e2331075fdb236ca19bf30ed6c -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — agent-eval: perplexity's #1 structural ask

> Auto-captured from an **approved** brain agenda item (#100121). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-20T10:04:35.910856Z · agenda #100121_

## The approved recommendation

Choose the single structural change: (a) publish signed, provenance-enveloped golden traces + token footprints for the four rail endpoints on the crawl surface and mirror the rail as one MCP workflow tool (recommended — origin-independent, builds on shipped provenance work); (b) relax/whitelist origin enforcement on the raw HTTP endpoints for verified external agents (higher security blast radius, and may not help if the block is harness-side); or (c) do both, sequenced (a) then (b). Also decide the regeneration cadence for golden traces so they don't drift from live schemas.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
