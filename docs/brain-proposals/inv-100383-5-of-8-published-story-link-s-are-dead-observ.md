<!-- fingerprint:bbc2cd59ae69b7e09e81b535238861b5 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — 5 of 8 published story link(s) are dead — observed from the none seat on media: https://dchub.cloud/press-release/2026-08-26-cheyenne-wyoming-wecc-power -> HTTP 404; https://dchub.cloud/press-release/auto-2026-08-26-coreweave-360-mw-global -> HTTP... What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100383). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-27T18:47:28.878223Z · inv #100383_

## The approved recommendation

Approve (1) re-triggering `gh workflow run press-rss.yml` in dchub-frontend now and re-probing all 8 published URLs afterward, and decide (2) whether to also fund the follow-up guard — making the auto-publisher verify HTTP 200 on a story URL before posting it — versus accepting the one-off bake re-run as sufficient. If the 404s persist post-bake, separately authorize an edge-routing inspection of /press-release/* in _routes.json/_worker.js.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
