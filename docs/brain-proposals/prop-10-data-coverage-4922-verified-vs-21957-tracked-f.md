<!-- fingerprint:0f27c40b02593f6830c1f62d4c50277b -->
# Brain proposal — [data_coverage] 4922 verified vs 21957 tracked facilities (17035 in the unverified discovery pile)

> Auto-captured from an **approved** brain prop item (#10). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-11T18:55:00.218815Z · prop #10_

## The approved recommendation

Decide the sequencing: (A) pause geographic prioritization and first fix/reconcile the verified-flag pipeline (is_duplicate/merged_at semantics vs canonical_stats), or (B) proceed now with the US/PJM cluster (2,163 facilities) as the first verification batch and the unknown-provider normalization pass (2,855 facilities) as the parallel workstream, accepting the risk that some are already verified under the canonical definition.

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