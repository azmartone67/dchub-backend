<!-- fingerprint:070dfee8f49c2cd18bfa1fcbd7955509 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: citation_score_below_30pct @ /api/v1/citations/score (seen x20)

> Auto-captured from an **approved** brain agenda item (#100123). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-21T18:11:04.694721Z · agenda #100123_

## The approved recommendation

Approve the three-part structural fix (scorer input-health instrumentation, citation_score added to HEALTH_BASELINE fences, detector dedup-with-escalation) versus continuing per-instance triage; and decide whether to pause the citation_score_below_30pct detector while the scorer is instrumented, accepting temporary blindness on this signal.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
