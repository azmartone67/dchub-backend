<!-- fingerprint:4a1bd26fcd240c91970c42ba28abca96 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — 1 published number(s) disagree with canon — observed from the anon seat on contract: / publishes [18000, 18300] for 'facilities'; 3 page/number pair(s) checked What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100224). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-18T21:59:14.072049Z · inv #100224_

## The approved recommendation

Choose the single canonical facilities floor to pin (a rounded value <= the confirmed live verified count, e.g. '18,400+' vs an exact live-computed figure), and approve extending the deals_phrase()-style consolidation (one facilities_phrase() helper + stale_markers entries for '18,000'/'18,300') across all three publishing surfaces — versus a quick hand-patch of the two divergent pages.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
