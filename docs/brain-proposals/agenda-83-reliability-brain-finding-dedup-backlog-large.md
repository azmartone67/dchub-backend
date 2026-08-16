# Brain proposal — [reliability] Brain finding: dedup_backlog_large @ /api/v1/facilities/delta (value 21,938)

> Auto-captured from an **approved** brain agenda item (#83). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-11T08:04:19.167600Z · agenda #83_

## The approved recommendation

Choose whether to (1) authorize an immediate investigation of the verified-count regression (0 vs 400 discrepancy) and repair of the dedup cron + merge logic in discovery_routes.py, and (2) approve redefining the dedup_backlog_large detector's healthy condition from a static value to a 7-day drain-rate SLO (verified slope positive, backlog shrinking) so the alert clears only on genuine throughput recovery, not on threshold adjustment.

## Triage — 2026-08-16 (spec-debt sweep #2) — CLOSED, exact re-file

Same condition as `docs/brain-proposals/agenda-69-reliability-brain-finding-dedup-backlog-large.md`, filed earlier and STILL OPEN as
the canonical obligation. The two differ only in the live counts embedded in
the title — identical once numbers are stripped. Closing this copy does not
close the obligation: act on agenda-69-reliability-brain-finding-dedup-backlog-large.md.

## Human checklist

- [x] Confirm this is still worth doing — the CONDITION is still open — this COPY is not; canonical doc is agenda-69-reliability-brain-finding-dedup-backlog-large.md
- [x] Scope it to a concrete change (file(s) + approach) — scoping belongs to agenda-69-reliability-brain-finding-dedup-backlog-large.md, which stays open
- [x] Implement + verify — not applicable to a duplicate — implement against agenda-69-reliability-brain-finding-dedup-backlog-large.md
- [x] Or close this PR if superseded / not worth it — closed 2026-08-16 as an exact re-file of agenda-69-reliability-brain-finding-dedup-backlog-large.md (spec-debt sweep #2)