<!-- fingerprint:09c075150b5d9f037a013ccb8c3a1e59 -->
# Brain proposal — [performance] top open item is class '(unknown)' (leverage 0.95) of 5 ranked

> Auto-captured from an **approved** brain agenda item (#38). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-06-30T02:32:02.253747Z · agenda #38_

## The approved recommendation

Approve building an upstream provider-normalization rule (canonical map + null-provider quarantine instead of '(unknown)' default) as the systemic fix, OR first commission a pull of the work_items/ranking schema to confirm '(unknown)' is the provider-attribution gap and to measure expected plan-share reduction before committing engineering effort.

## Triage — 2026-08-14 (spec-debt sweep, oldest-five · PR #2673)

**CLOSED via the recommendation's own second branch (confirm-and-measure),
which also refutes the first branch's premise.**

- Condition re-verified live 2026-08-14: `GET /api/v1/brain/work-plan` top
  item id=140, class `(unknown)`, leverage 0.95 — the same signal as filed.
- Schema pull done: `(unknown)` is **not** a provider-attribution gap. It is
  the fix-class fallback in `routes/brain_work_selector.py::leverage_score` /
  `_candidate_class`, emitted when an open proposal carries no
  `klass`/`class`/`_class`/`finding_class` key and `classify_mechanical`
  returns nothing. A provider canonical-map / null-provider quarantine would
  not move it — that branch is mis-scoped and is deliberately NOT built.
- The measure the recommendation demanded before committing engineering
  effort is implemented in PR #2673: the work-plan response now carries
  `unclassified_share {count, of, share, basis}` (share=None on an empty
  plan — UNMEASURED, never 0). Mutation-tested in
  `tests/test_brain_work_selector.py`.
- Any future classification-coverage build should be a NEW spec citing the
  measured share, per this recommendation's own "measure expected plan-share
  reduction before committing engineering effort" clause.

## Human checklist

- [x] Confirm this is still worth doing — confirmed live 2026-08-14 (top work-plan item still class '(unknown)' lev 0.95); see Triage above (PR #2673)
- [x] Scope it to a concrete change (file(s) + approach) — scoped to routes/brain_work_selector.py `unclassified_share` measurement; provider-normalization branch refuted as mis-premised (PR #2673)
- [x] Implement + verify — shipped in PR #2673; mutation-tested (tests/test_brain_work_selector.py, 20 passed)
- [x] Or close this PR if superseded / not worth it — closed 2026-08-14: measure-first branch complete, evidence above (PR #2673)
