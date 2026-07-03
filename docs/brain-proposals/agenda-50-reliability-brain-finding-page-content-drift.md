# Brain proposal — [reliability] Brain finding: page_content_drift:/admin/funnel-health @ /admin/funnel-health (seen x288)

> Auto-captured from an **approved** brain agenda item (#50). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-03T09:40:51.400991Z · agenda #50_

## The approved recommendation

Approve (a) moving /admin/* dashboard routes from content-hash drift to structural-diff monitoring with volatile-region exclusions, and (b) adding signature-based dedup/aggregation to the Brain findings pipeline — or decide instead to simply mute this one route, accepting that the same noise pattern (cf. 2,559 freshness-breach findings) will keep recurring elsewhere.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
