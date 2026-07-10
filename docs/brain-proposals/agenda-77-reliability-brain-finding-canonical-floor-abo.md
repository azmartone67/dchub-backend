# Brain proposal — [reliability] Brain finding: canonical_floor_above_live_reality @ canonical_stats._FALLBACK (seen x29)

> Auto-captured from an **approved** brain agenda item (#77). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-10T04:02:17.320775Z · agenda #77_

## The approved recommendation

Choose the interpretation that drives the fix: (A) verified=1 is ground truth → lower/auto-derive floors and accept the collapsed verified count, or (B) verified=1 is the defect → prioritize clearing the 21,929-item dedup backlog and keep floors as one-shot tripwires. Then approve (or reject) replacing static _FALLBACK constants with last-known-good snapshots plus a clamp-and-alert-once invariant, and finding-level dedup so a persistent condition yields one open finding instead of 29.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
