<!-- fingerprint:80d027f664640cbeaf84187b6242af4a -->
# Brain proposal — [reliability] Brain finding: canonical_floor_above_live_reality @ canonical_stats._FALLBACK (seen x29)

> Auto-captured from an **approved** brain agenda item (#77). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-10T04:02:17.320775Z · agenda #77_

## The approved recommendation

Choose the interpretation that drives the fix: (A) verified=1 is ground truth → lower/auto-derive floors and accept the collapsed verified count, or (B) verified=1 is the defect → prioritize clearing the 21,929-item dedup backlog and keep floors as one-shot tripwires. Then approve (or reject) replacing static _FALLBACK constants with last-known-good snapshots plus a clamp-and-alert-once invariant, and finding-level dedup so a persistent condition yields one open finding instead of 29.

## Rolled-up targets — class `canonical_floor_above_live_reality` (class collapse, 2026-08-17)

This doc is now the single obligation for **4 occurrences** of
`canonical_floor_above_live_reality`. The other 3 were closed against it. They are listed here
in full so the collapse loses no target — fixing the class means fixing
every line below, and a fix that only covers this doc's own target has not
discharged the obligation.

- `canonical_stats._FALLBACK` — was `agenda-77-reliability-brain-finding-canonical-floor-abo.md` (filed 2026-07-10)
- `ai_surface_canon.PINNED.public` — was `agenda-100172-reliability-brain-finding-canonical-floor-abo.md` (filed 2026-08-05)
- `ai_surface_canon.PINNED.public` — was `inv-100052-canonical-floor-above-live-reality-observed-at.md` (filed 2026-08-09)
- `ai_surface_canon.PINNED.public` — was `inv-100053-canonical-floor-above-live-reality-observed-at.md` (filed 2026-08-09)

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
