<!-- fingerprint:3e93f1274c99658ca201e9cc929f65f8 -->
# Brain proposal — [reliability] Brain finding: page_content_drift:/api/v1/admin/qa/state-of-2026-precheck @ /api/v1/admin/

> Auto-captured from an **approved** brain agenda item (#66). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-07T03:40:08.860658Z · agenda #66_

## The approved recommendation

Two choices: (1) Inspect the current /api/v1/admin/qa/state-of-2026-precheck response once and decide: accept it as the new baseline (rebaseline the hash) or treat it as a regression and fix the endpoint. (2) Approve the structural detector change: fingerprint-deduplicated findings with rebaseline-on-acknowledge, plus schema/data-contract validation instead of full-body hash for dynamic API pages — and decide whether to apply this pattern to the other high-count detectors (dedup_pipeline_stalled, data_freshness_sla_breach) in the same change or separately.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
