<!-- fingerprint:924d4aa7c8467513cd667ba540f85873 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — Zero real humans have ever opened a relay link (relay_opens holds only our own probe traffic) while the write path is proven functional. What single change makes an agent actually hand the link to its human?

> Auto-captured from an **approved** brain inv item (#100017). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-28T07:21:52.339519Z · inv #100017_

## The approved recommendation

Approve degrading gated tool responses to partial-answer-plus-mandatory-link (forcing the link into the agent's answer text) versus keeping full responses with the link as optional metadata — i.e., accept a deliberate free-tier utility cut to force human handoff, and confirm whether relay_opens instrumentation should ship first to validate the zero-human-opens premise before the response-structure change.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
