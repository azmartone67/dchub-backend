<!-- fingerprint:4df5053bc42dd184c1d206fb0bbd1887 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — work-plan: lift the '(unknown)' fix-class

> Auto-captured from an **approved** brain agenda item (#100135). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-24T08:17:07.175046Z · agenda #100135_

## The approved recommendation

Approve building an ingest-time canonical normalization gate (alias dictionary + schema validation + enrichment quarantine for unclassified rows) as the single systemic fix, versus continuing per-instance '(unknown)' cleanup — and confirm which field/table the work-plan's '(unknown)' class actually reads from before scoping the gate.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
