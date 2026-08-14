# Brain proposal — [data_coverage] 311 DCPI markets across 7 live ISOs

> Auto-captured from an **approved** brain agenda item (#41). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-01T00:49:01.512609Z · agenda #41_

## The approved recommendation

Decide whether to fund extending live grid telemetry to additional high-load US ISOs (breadth) versus deepening data within already-covered markets — and first commission the missing ISO onboarding-cost and per-market demand data needed to confirm this breadth-leaning recommendation.

## Triage — 2026-08-14 (spec-debt sweep, oldest-five · PR #2673) — BLOCKED

**Real work, but it needs an owner decision that does not exist yet. Boxes
left unchecked ON PURPOSE — this row remains open debt until the decision is
recorded here.**

- Condition re-verified 2026-08-14: still 7 independent live grid-telemetry
  feeds (49 grid regions); DCPI markets 300+ per `/api/v1/canon/phrases` —
  substantially the same breadth as at filing.
- The obligation is a FUNDING decision (extend live telemetry to additional
  high-load US ISOs vs deepen already-covered markets) plus commissioning
  ISO onboarding-cost and per-market demand data that exists nowhere in the
  repo or DB today. Neither is implementable as an engineering change alone.
- BLOCKER: needs an explicit owner call on breadth-vs-depth recorded in this
  doc. Note the 2026-08 platform direction has in practice favored deepening
  /distribution, but no decision has been recorded against this spec.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
