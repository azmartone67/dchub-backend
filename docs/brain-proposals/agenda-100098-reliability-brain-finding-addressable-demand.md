<!-- fingerprint:d0e79233a0a689d5c814a5ed5afca3f7 -->
# Brain proposal — [reliability] Brain finding: addressable_demand_unconverted @ tool:get_fiber_intel (seen x162)

> Auto-captured from an **approved** brain agenda item (#100098). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-15T07:48:20.966260Z · agenda #100098_

## The approved recommendation

Approve changing the addressable_demand_unconverted finding from a re-firing aggregate alert to a stateful per-user finding with a defined resolution lifecycle, and authorize wiring it to the existing _bind/bind_email nudge surface (a user-facing outreach action affecting 162 free users) — versus keeping it as a passive escalation-only signal.

## Rolled-up targets — class `addressable_demand_unconverted` (class collapse, 2026-08-17)

This doc is now the single obligation for **4 occurrences** of
`addressable_demand_unconverted`. The other 3 were closed against it. They are listed here
in full so the collapse loses no target — fixing the class means fixing
every line below, and a fix that only covers this doc's own target has not
discharged the obligation.

- `tool:get_fiber_intel` — was `agenda-100098-reliability-brain-finding-addressable-demand.md` (filed 2026-07-15)
- `tool:get_grid_intelligence` — was `agenda-100134-reliability-brain-finding-addressable-demand.md` (filed 2026-07-24)
- `tool:analyze_site` — was `agenda-100163-reliability-brain-finding-addressable-demand.md` (filed 2026-08-03)
- `tool:analyze_site` — was `inv-100067-addressable-demand-unconverted-observed-at-too.md` (filed 2026-08-10)

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
