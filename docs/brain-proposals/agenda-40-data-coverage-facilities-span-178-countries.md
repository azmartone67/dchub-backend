<!-- fingerprint:5f85d2a8d901c30f07795115407cd50a -->
# Brain proposal — [data_coverage] facilities span 178 countries

> Auto-captured from an **approved** brain agenda item (#40). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-01T00:49:07.298507Z · agenda #40_

## The approved recommendation

Decide whether to (A) redirect ingestion-prioritization effort toward clearing the 21,422-facility verification/dedup backlog in already-tracked top markets, or (B) fund the missing per-country market-size + ingestion-effort + competitor-coverage datasets needed to actually rank under-covered countries by moat-per-effort. The current evidence supports (A) and cannot yet support (B).

## Triage — 2026-08-14 (spec-debt sweep, oldest-five · PR #2673)

**CLOSED — the decision this spec obligated was taken and executed as (A),
and the headline number itself was re-based by later work.**

- (A) executed: the verification/dedup backlog the recommendation pointed at
  (21,422 unverified) is ≈7,683 live today (`GET /api/v1/stats`:
  `discovered_verified=17,946` of `discovered_total=25,629`), with
  cross-source dedup now the published count basis
  (`facilities_distinct=17,898`, duplicate_of_id IS NULL).
- The "178 countries" figure was re-based 2026-07-30 by the honest-numbers
  canon (`ai_surface_canon.py` — the deduped fleet spans 178 distinct codes
  incl. territories; the legacy 186 double-counted 9 full-name/ISO-code
  pairs; public floor "170+"). Live 2026-08-14: `/api/v1/stats`
  countries=178; `/api/v1/canon/phrases` countries="170+".
- (B) — funding per-country market-size / ingestion-effort /
  competitor-coverage datasets — was NOT pursued and those datasets still do
  not exist. If country-expansion ranking is ever wanted, file a fresh spec
  against current numbers; this doc's basis is stale.

## Human checklist

- [x] Confirm this is still worth doing — NO: the (A)-vs-(B) decision is resolved; (A) was executed by the dedup/suppression waves (live numbers in Triage, PR #2673)
- [x] Scope it to a concrete change (file(s) + approach) — superseded: scope = decision (A), delivered by the landed dedup/verification work + 2026-07-30 country-basis fix in ai_surface_canon.py (PR #2673)
- [x] Implement + verify — verified live 2026-08-14: /api/v1/stats countries=178 & backlog ≈7,683; /api/v1/canon/phrases countries="170+" (PR #2673)
- [x] Or close this PR if superseded / not worth it — closed 2026-08-14 as decided-and-executed with live evidence (PR #2673)
