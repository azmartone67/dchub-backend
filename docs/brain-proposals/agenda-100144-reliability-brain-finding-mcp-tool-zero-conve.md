<!-- fingerprint:c9cfc8703d2e3a65460455fbf4a82aa1 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: mcp_tool_zero_conversion @ /admin/per-tool-conversion#get_grid_data (seen x

> Auto-captured from an **approved** brain agenda item (#100144). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-27T03:27:16.337433Z · agenda #100144_

## The approved recommendation

Choose between: (1) approve a detector redesign (expected-conversion statistical gate + finding fingerprint/dedup with cooldown + cross-check against the Stripe-backed conversions source before emitting), retiring the per-tool 'improve description' patch stream; or (2) keep the current per-tool detector and continue patching each tool's copy individually. Also decide whether to bulk-close the existing ~1,400+ accumulated duplicate findings across the 12 affected tools as noise once the fix ships, and confirm whether the 2026-07-17 shipped fixes already cover part of this scope.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
