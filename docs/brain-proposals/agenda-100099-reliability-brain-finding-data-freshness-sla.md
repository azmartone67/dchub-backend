# Brain proposal — [reliability] Brain finding: data_freshness_sla_breach @ table:usgs_water_stress (seen x2847)

> Auto-captured from an **approved** brain agenda item (#100099). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-15T07:48:06.762403Z · agenda #100099_

## The approved recommendation

Approve the two-layer structural fix: (a) repair/restart the usgs_water_stress cron after checking Railway logs to confirm failure mode, and (b) authorize engineering work on cron dead-man's-switch monitoring plus fingerprint-based finding deduplication for all recurring detector types — or choose to only patch the single cron and accept the class recurring elsewhere.

## Triage — 2026-08-16 (spec-debt sweep #2) — CLOSED, condition already adjudicated

This condition was already decided and closed with live evidence in
`docs/brain-proposals/agenda-48-reliability-brain-finding-data-freshness-sla.md` (spec-debt sweep, PR #2673, 2026-08-14). This doc
restates it; it is closed AGAINST that adjudication rather than re-decided.
Boxes follow the PR #2673 convention: all four carry the outcome, including the
branch not taken.

## Human checklist

- [x] Confirm this is still worth doing — NO — already adjudicated in agenda-48-reliability-brain-finding-data-freshness-sla.md (PR #2673, 2026-08-14); not re-decided here
- [x] Scope it to a concrete change (file(s) + approach) — superseded — scope was settled in agenda-48-reliability-brain-finding-data-freshness-sla.md
- [x] Implement + verify — no new implementation; agenda-48-reliability-brain-finding-data-freshness-sla.md carries the live evidence
- [x] Or close this PR if superseded / not worth it — closed 2026-08-16 as already-adjudicated (spec-debt sweep #2)