# Brain proposal — [reliability] Brain finding: frontend_endpoint_slow @ /construction-pipeline (value 6,642)

> Auto-captured from an **approved** brain agenda item (#91). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-13T05:25:29.155482Z · agenda #91_

## The approved recommendation

Decide whether to (a) authorize a profiling pass on the shared pipeline-data path behind /construction-pipeline and /ai-pipeline (with caching/pagination as the likely fix), or (b) first pull HEALTH_BASELINE.md's frontend_endpoint_slow threshold plus recent deploy/change history to rule out an infrastructure or deployment regression before touching application code.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
