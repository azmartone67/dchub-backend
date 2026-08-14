<!-- fingerprint:781c40847668ebf5322898970e3954e5 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: mcp_tool_zero_conversion @ /admin/per-tool-conversion#rank_markets (seen x1

> Auto-captured from an **approved** brain agenda item (#100194). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-14T19:12:37.367177Z · agenda #100194_

## The approved recommendation

Choose between: (A) redesign the detector — fingerprint dedup + base-rate-aware firing threshold + roll-up to one funnel-level finding (stops recurrence structurally, ~engineering effort on the brain's detector pipeline); (B) a lighter patch — bulk-close the ~1,900 open duplicates and raise the detector's lookback/threshold only (cheap, but risks re-accumulation); or (C) keep the per-tool findings but require the human to confirm the 2026-07-17 fixes' scope first before any new work. Also decide whether the signals→codes 100% drop warrants its own dedicated investigation, since that — not per-tool copy — appears to be the real conversion bottleneck.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
