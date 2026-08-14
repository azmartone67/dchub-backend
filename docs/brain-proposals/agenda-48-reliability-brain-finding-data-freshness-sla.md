# Brain proposal — [reliability] Brain finding: data_freshness_sla_breach @ table:usgs_water_stress (seen x2547)

> Auto-captured from an **approved** brain agenda item (#48). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-02T23:29:31.088504Z · agenda #48_

## The approved recommendation

Choose the remediation path: (1) implement stateful open/close breach findings for freshness (and likely all high-count detectors) as the class-level fix, (2) first pull the usgs_water_stress SLA config, ingestion job logs, and breach timestamps to confirm whether this is chronic staleness vs. repeated pipeline failures before changing alerting, or (3) both in parallel. Also decide whether the freshness SLA should be recalibrated to USGS's actual publication cadence.

## Triage — 2026-08-14 (spec-debt sweep, oldest-five · PR #2673)

**CLOSED — superseded: every remediation path this spec offered was
completed by later, independent landed work.**

1. The investigation (option 2) happened and found the root cause: the
   `usgs_water_stress` table was superseded 2026-07-10 by the WRI Aqueduct
   4.0 ingest (`routes/water_aqueduct_ingest.py`, PR #1500) and its
   producing cron is gone — the detector was firing on a table with nothing
   left to fix (2,800+ times).
2. The SLA-recalibration decision (option 3's second half) was taken: the
   usgs_water_stress SLA (504h) was RETIRED from
   `check_data_freshness_sla_breach` on 2026-07-16 (PR #1941 — dated comment
   in the SLAS list, `routes/brain_consistency_radar.py`). WRI Aqueduct is
   deliberately NOT SLA-watched: annual publication cadence, so any
   hours-scale SLA would mint a new false-alarm stream.
3. The class-level high-count fix (option 1) landed as occurrence TYPING
   rather than stateful open/close findings: freshness breaches now declare
   `count_kind="hours"` (magnitude, not a recurrence tally) and the work
   selector refuses to read untyped six-figure counts as tallies
   (`VALUE_NOT_COUNT_ISSUES` / `occurrence_signal` / untyped ceiling —
   PR #2143 lane 3). The "seen x2547" misread that minted this very agenda
   item is the documented motivating case in
   `routes/brain_work_selector.py`.

Verified 2026-08-14 against origin/main: the SLAS list in
`check_data_freshness_sla_breach` contains no usgs_water_stress entry, and
the current platform self-alarm set contains no usgs_water_stress freshness
breach.

## Human checklist

- [x] Confirm this is still worth doing — NO: breach source removed 2026-07-16 (PR #1941); table superseded by WRI Aqueduct (PR #1500); see Triage above (PR #2673)
- [x] Scope it to a concrete change (file(s) + approach) — superseded: scope was delivered by PRs #1500 (WRI ingest), #1941 (SLA retire), #2143 (occurrence typing)
- [x] Implement + verify — implemented in the PRs above; re-verified 2026-08-14 against origin/main (no usgs_water_stress in the SLAS list) (PR #2673)
- [x] Or close this PR if superseded / not worth it — closed 2026-08-14 as superseded, evidence above (PR #2673)
