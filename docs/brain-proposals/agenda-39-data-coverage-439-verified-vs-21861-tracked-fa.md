<!-- fingerprint:0f27c40b02593f6830c1f62d4c50277b -->
# Brain proposal — [data_coverage] 439 verified vs 21861 tracked facilities (21422 in the unverified discovery pile)

> Auto-captured from an **approved** brain agenda item (#39). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-06-30T08:55:16.622049Z · agenda #39_

## The approved recommendation

Approve a verification sprint targeting the US/PJM cluster plus a provider-normalization + bulk-verify pass on Digital Realty/Equinix/AWS (with Brazil as a secondary zero-coverage target), OR request a per-facility effort/data-completeness estimate first to confirm leverage before committing resources.

## Triage — 2026-08-14 (spec-debt sweep, oldest-five · PR #2673)

**CLOSED — obsolete: the condition no longer exists. The verification sprint
this spec proposed effectively happened in the July–August waves.**

- At filing (2026-06-30): 439 of 21,861 tracked facilities verified (2.0%),
  21,422 in the unverified pile.
- Live 2026-08-14 (`GET /api/v1/stats` → `_facility_count_notes`):
  `discovered_verified=17,946` of `discovered_total=25,629` raw rows
  (~70% verified). Unverified pile ≈7,683 — down ~64% from 21,422.
- The provider-normalization + dedup pass the recommendation asked for
  landed as the dedup/suppression waves: the published count basis is now
  "distinct sites after cross-source de-duplication (duplicate_of_id IS
  NULL)" with `facilities_distinct=17,898`.
- Residual unverified rows are ordinary pipeline inventory watched by the
  ingestion/deadman surfaces; no sprint is left to commission at the
  leverage this spec described.

## Human checklist

- [x] Confirm this is still worth doing — NO: condition obsolete; verified count moved 439 → 17,946 (live /api/v1/stats, 2026-08-14; Triage above, PR #2673)
- [x] Scope it to a concrete change (file(s) + approach) — superseded: the scoped work (verification + provider-normalization/dedup pass) already landed via the duplicate_of_id suppression waves (see Triage, PR #2673)
- [x] Implement + verify — verified live 2026-08-14 against /api/v1/stats (numbers above, PR #2673); no new implementation needed
- [x] Or close this PR if superseded / not worth it — closed 2026-08-14 as obsolete with live evidence (PR #2673)
