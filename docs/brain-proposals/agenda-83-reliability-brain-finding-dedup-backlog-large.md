# Brain proposal — [reliability] Brain finding: dedup_backlog_large @ /api/v1/facilities/delta (value 21,938)

> Auto-captured from an **approved** brain agenda item (#83). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-11T08:04:19.167600Z · agenda #83_

## The approved recommendation

Choose whether to (1) authorize an immediate investigation of the verified-count regression (0 vs 400 discrepancy) and repair of the dedup cron + merge logic in discovery_routes.py, and (2) approve redefining the dedup_backlog_large detector's healthy condition from a static value to a 7-day drain-rate SLO (verified slope positive, backlog shrinking) so the alert clears only on genuine throughput recovery, not on threshold adjustment.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
