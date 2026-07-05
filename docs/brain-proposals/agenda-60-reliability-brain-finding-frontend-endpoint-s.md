# Brain proposal — [reliability] Brain finding: frontend_endpoint_slow @ /capacity-pipeline (value 6,819)

> Auto-captured from an **approved** brain agenda item (#60). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-05T21:07:37.995070Z · agenda #60_

## The approved recommendation

Confirm the unit and SLO threshold for frontend_endpoint_slow (from the detector code / HEALTH_BASELINE.md), then choose: (a) approve the structural fix — pool sizing/connection-reuse remediation plus cached/precomputed /capacity-pipeline responses — or (b) defer and instead instrument the endpoint (dependency timing traces + 7–30d trend) first to verify the pool-saturation hypothesis before committing engineering effort. Do NOT approve any option that merely raises the detection threshold.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
