<!-- fingerprint:db2d61c23f280d421e155821bb5b6704 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [developer_ux] Brain finding: ai_surface_drift:llms_full:stale_value @ https://dchub.cloud/llms-full.txt

> Auto-captured from an **approved** brain prop item (#100067). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-25T04:09:33.155599Z · prop #100067_

## The approved recommendation

Approve building a canonical-stats-driven render pipeline for ALL AI surfaces with a deploy-time diff gate (one-time engineering investment that retires the ai_surface_drift finding class), versus continuing to auto-patch each stale surface as detectors flag it. If approved, also decide whether the detector semantics change from value-diffing to pipeline-freshness verification.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
