# Brain proposal — [reliability] Brain finding: frontend_endpoint_slow @ /capacity-pipeline (value 6,472)

> Auto-captured from an **approved** brain agenda item (#75). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-09T06:28:12.940989Z · agenda #75_

## The approved recommendation

Choose the remediation path: (1) approve root-cause work — process the 21,911-facility dedup backlog and add a cached/precomputed aggregate for /capacity-pipeline (and /construction-pipeline) — versus (2) merely adjusting the frontend_endpoint_slow threshold. Also confirm from HEALTH_BASELINE.md what the healthy latency target actually is before committing engineering effort.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
