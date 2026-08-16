# Brain proposal — [data_coverage] 459 verified vs 21881 tracked facilities (21422 in the unverified discovery pile)

> Auto-captured from an **approved** brain agenda item (#52). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-03T09:40:39.152003Z · agenda #52_

## The approved recommendation

Decide whether to (a) allocate engineering time now to unstall the dedup pipeline at /api/v1/facilities/delta before any manual verification effort, and (b) approve the post-fix priority order: operator-portfolio verification (Digital Realty, Equinix, AWS — with provider-name normalization first) followed by geographic focus on US/PJM and Brazil, versus an alternative demand-driven ordering once query analytics are gathered.

## Triage — 2026-08-16 (spec-debt sweep #2) — CLOSED, condition already adjudicated

This condition was already decided and closed with live evidence in
`docs/brain-proposals/agenda-39-data-coverage-439-verified-vs-21861-tracked-fa.md` (spec-debt sweep, PR #2673, 2026-08-14). This doc
restates it; it is closed AGAINST that adjudication rather than re-decided.
Boxes follow the PR #2673 convention: all four carry the outcome, including the
branch not taken.

## Human checklist

- [x] Confirm this is still worth doing — NO — already adjudicated in agenda-39-data-coverage-439-verified-vs-21861-tracked-fa.md (PR #2673, 2026-08-14); not re-decided here
- [x] Scope it to a concrete change (file(s) + approach) — superseded — scope was settled in agenda-39-data-coverage-439-verified-vs-21861-tracked-fa.md
- [x] Implement + verify — no new implementation; agenda-39-data-coverage-439-verified-vs-21861-tracked-fa.md carries the live evidence
- [x] Or close this PR if superseded / not worth it — closed 2026-08-16 as already-adjudicated (spec-debt sweep #2)