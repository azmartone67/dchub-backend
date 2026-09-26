<!-- fingerprint:7b9132e37a4a7447cd6c0b49a69d6a40 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: citation_score_dropped @ /api/v1/citations/score (seen x20)

> Auto-captured from an **approved** brain agenda item (#100129). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-22T22:58:09.690171Z · agenda #100129_

## The approved recommendation

Approve building score-history instrumentation for /api/v1/citations/score as the first-class fix (with detector recalibration to sustained-breach alerting), OR direct an immediate deep-dive into the compute/upstream path now, accepting that without persisted history the next investigation will likely hit the same 'cause not measured' wall as the prior six findings.

## Rolled-up targets — class `citation_score_dropped` (class collapse, 2026-09-26)

This doc is now the single obligation for **2 occurrences** of
`citation_score_dropped`. The other 1 were closed against it. They are listed here
in full so the collapse loses no target — fixing the class means fixing
every line below, and a fix that only covers this doc's own target has not
discharged the obligation.

- `/api/v1/citations/score` — was `agenda-100129-reliability-brain-finding-citation-score-drop.md` (filed 2026-07-22)
- `/api/v1/citations/score` — was `inv-100375-citation-score-dropped-observed-at-api-v1-cit.md` (filed 2026-08-25)

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
