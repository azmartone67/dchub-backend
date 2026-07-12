# Brain proposal — [reliability] Brain finding: mcp_funnel_concentration_top5 @ /api/v1/mcp/funnel (seen x808)

> Auto-captured from an **approved** brain agenda item (#87). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-12T07:47:22.283396Z · agenda #87_

## The approved recommendation

Approve reclassifying mcp_funnel_concentration_top5 from a per-scan alerting detector to a baselined metric with change-only alerting (and dedup of stable-state re-fires), versus keeping it as a recurring alert. If approved, also decide the deviation threshold (e.g., what shift in top-5 share should re-trigger) and confirm the concentration feed should be wired into the existing paywall/trial flow rather than spawning new build work.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
