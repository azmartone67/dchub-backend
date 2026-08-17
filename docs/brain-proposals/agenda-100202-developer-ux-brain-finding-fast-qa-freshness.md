<!-- fingerprint:bf8cfeef96ff2b1500237c8bf61c0548 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [developer_ux] Brain finding: fast_qa_freshness_drift:news @ dchub://freshness/news (seen x1)

> Auto-captured from an **approved** brain agenda item (#100202). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-17T08:00:34.448903Z · agenda #100202_

## The approved recommendation

Choose whether to (a) approve a two-part structural fix — instrument the news ingestion job and add a pre-SLA staleness watchdog that auto-re-triggers ingestion, plus enforce the substance-gate (no closure without a linked code/config change) for the freshness_drift class — or (b) first commission a verification pass to confirm whether the 2026-07-01 media fix should already cover this path and this re-fire indicates a regression.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
