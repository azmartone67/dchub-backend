<!-- fingerprint:6d1edd0de2f7f8b940b208577432f8c1 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — 1 published number(s) disagree with canon — observed from the anon seat on contract: /pricing: facilities=21,200 > canon floor 20,700; 3 page/number pair(s) checked What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100591). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-09T23:42:36.660291Z · inv #100591_

## The approved recommendation

Locate the pinned facilities floor constant (currently 20,700) in the canon/ai_surface_canon config and raise it to 21,265 to match the deduped live verified count (canonical_stats, issue #1539 filter), then re-run the /pricing render to confirm the published 21,200 no longer trips the floor-divergence detector.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
