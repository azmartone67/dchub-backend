<!-- fingerprint:934e0c0fe4a4900df17bcaab97dee3fe -->
# Brain proposal — [reliability] Brain finding: mcp_tool_zero_conversion @ /admin/per-tool-conversion#get_grid_intelligence

> Auto-captured from an **approved** brain agenda item (#76). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-09T06:28:09.406041Z · agenda #76_

## The approved recommendation

Approve reworking the mcp_tool_zero_conversion detector into a stateful, base-rate-aware, multi-touch-attributed check (one aggregated finding instead of per-tool re-fires) — versus keeping the current per-tool detector and continuing to treat each of the 175+ instances as individual copy/messaging fixes.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
