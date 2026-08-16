<!-- fingerprint:0f27c40b02593f6830c1f62d4c50277b -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [data_coverage] 4965 verified vs 22001 tracked facilities (17036 in the unverified discovery pile)

> Auto-captured from an **approved** brain agenda item (#100115). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-19T12:11:07.370862Z · agenda #100115_

## The approved recommendation

Approve the sequencing choice: run the US ISO-cluster sweep (PJM first, then WECC) as the primary verification batch, with the Digital Realty/Equinix provider-batch as the parallel second track — or redirect effort to international rate-laggards like Brazil (13% verified) if geographic breadth matters more than absolute conversion volume. Also decide whether provider-name normalization (Equinix dedupe) should ship before the provider batch runs.

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
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-16 as already-adjudicated (spec-debt sweep #2)