<!-- fingerprint:8e81d52ae35c098d9c8fae8db1211018 -->
# Brain proposal — [reliability] Brain finding: data_freshness_sla_breach @ table:usgs_water_stress (seen x2736)

> Auto-captured from an **approved** brain agenda item (#81). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-10T19:37:56.278090Z · agenda #81_

## The approved recommendation

Approve the two-part fix: (a) authorize investigation of Railway logs and re-registration/hardening of the usgs_water_stress cron with retry + missed-window heartbeat, and (b) approve changing detector semantics to one deduplicated finding per breach episode (which will collapse the 2,736-entry backlog) — or choose to keep per-cycle findings and only fix the cron.

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