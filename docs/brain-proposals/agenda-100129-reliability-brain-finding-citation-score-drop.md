<!-- fingerprint:7b9132e37a4a7447cd6c0b49a69d6a40 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: citation_score_dropped @ /api/v1/citations/score (seen x20)

> Auto-captured from an **approved** brain agenda item (#100129). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-22T22:58:09.690171Z · agenda #100129_

## The approved recommendation

Approve building score-history instrumentation for /api/v1/citations/score as the first-class fix (with detector recalibration to sustained-breach alerting), OR direct an immediate deep-dive into the compute/upstream path now, accepting that without persisted history the next investigation will likely hit the same 'cause not measured' wall as the prior six findings.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
