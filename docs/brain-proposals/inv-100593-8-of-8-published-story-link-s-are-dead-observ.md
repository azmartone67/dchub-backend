<!-- fingerprint:1a90fa34a43f34e89181639bd0d41877 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — 8 of 8 published story link(s) are dead — observed from the none seat on media: https://dchub.cloud/press-release/2026-09-08-spp-commands-five-top-eight-excess-power-markets -> HTTP 404; https://dchub.cloud/press-release/ercot-438gw-queue-multi-ye... What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100593). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-09T23:42:24.626826Z · inv #100593_

## The approved recommendation

Run `gh workflow run press-rss.yml` in the dchub-frontend repo to re-trigger the press bake, then curl one of the 8 slugs (https://dchub.cloud/press-release/2026-09-08-spp-commands-five-top-eight-excess-power-markets) to confirm it returns 200; if it still 404s, inspect scripts/bake_press_static.py to confirm it emits per-slug /press-release/<slug> pages and not just press.html + dc-hub-media/index.html.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
