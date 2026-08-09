<!-- fingerprint:7738e471fcd5c025e5d38eb0db3a58f8 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: mcp_tool_zero_conversion @ /admin/per-tool-conversion#get_water_risk (seen

> Auto-captured from an **approved** brain agenda item (#100183). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-09T19:46:29.270067Z · agenda #100183_

## The approved recommendation

Approve redesigning the mcp_tool_zero_conversion detector (fingerprint-based suppression + expected-conversion significance gate + single platform-level rollup finding), and decide whether to bulk-close the ~2,000+ accumulated per-tool findings as superseded — versus keeping the per-tool detector and continuing to triage each instance. Also decide whether to first audit why the 2026-07-17 fix (brain_findings/9736) did not stop the accumulation.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
