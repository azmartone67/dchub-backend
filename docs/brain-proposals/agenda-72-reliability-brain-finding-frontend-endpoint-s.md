# Brain proposal — [reliability] Brain finding: frontend_endpoint_slow @ /construction-pipeline (value 6,508)

> Auto-captured from an **approved** brain agenda item (#72). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-09T06:28:20.579073Z · agenda #72_

## The approved recommendation

Choose the remediation path: (1) prioritize clearing the 21,911-item dedup backlog so pipeline pages query the small verified set, (2) ship a cached/precomputed aggregate layer for /construction-pipeline, /capacity-pipeline, and /system-status without waiting on dedup, or (3) first run a profiling trace to confirm the bottleneck before committing engineering effort. Also decide whether the stuck Cloudflare Pages deploys (×10) must be unblocked first.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
