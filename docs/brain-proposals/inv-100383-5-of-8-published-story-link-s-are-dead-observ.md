<!-- fingerprint:bbc2cd59ae69b7e09e81b535238861b5 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — 5 of 8 published story link(s) are dead — observed from the none seat on media: https://dchub.cloud/press-release/2026-08-26-cheyenne-wyoming-wecc-power -> HTTP 404; https://dchub.cloud/press-release/auto-2026-08-26-coreweave-360-mw-global -> HTTP... What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100383). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-27T18:47:28.878223Z · inv #100383_

## The approved recommendation

Approve (1) re-triggering `gh workflow run press-rss.yml` in dchub-frontend now and re-probing all 8 published URLs afterward, and decide (2) whether to also fund the follow-up guard — making the auto-publisher verify HTTP 200 on a story URL before posting it — versus accepting the one-off bake re-run as sufficient. If the 404s persist post-bake, separately authorize an edge-routing inspection of /press-release/* in _routes.json/_worker.js.

## Rolled-up targets — class `media_story_links_dead` (class collapse, 2026-09-26)

This doc is now the single obligation for **14 occurrences** of
`media_story_links_dead`. The other 13 were closed against it. They are listed here
in full so the collapse loses no target — fixing the class means fixing
every line below, and a fix that only covers this doc's own target has not
discharged the obligation.

- `https://dchub.cloud/press-release/2026-08-26-cheyenne-wyoming-wecc-power -> HTTP 404; https://dchub.cloud/press-release/auto-2026-08-26-coreweave-360-mw-global …` — was `inv-100383-5-of-8-published-story-link-s-are-dead-observ.md` (filed 2026-08-27)
- `https://dchub.cloud/press-release/afternoon-pulse-2026-08-27-upper-peninsula-third -> HTTP 404; https://dchub.cloud/press-release/2026-08-26-cheyenne-wyoming-we…` — was `inv-100387-5-of-8-published-story-link-s-are-dead-observ.md` (filed 2026-08-28)
- `https://dchub.cloud/press-release/2026-08-29-kansas-city-spp-73-excess-power -> HTTP 404; https://dchub.cloud/press-release/auto-2026-08-29-neso-queue-600-gw-ti…` — was `inv-100397-5-of-8-published-story-link-s-are-dead-observ.md` (filed 2026-08-30)
- `https://dchub.cloud/press-release/2026-08-30-neso-queue-600-gw-uk-delay -> HTTP 404; https://dchub.cloud/press-release/2026-08-29-kansas-city-spp-73-excess-powe…` — was `inv-100399-6-of-8-published-story-link-s-are-dead-observ.md` (filed 2026-08-31)
- `https://dchub.cloud/press-release/auto-2026-08-31-neso-600gw-queue-timeline -> HTTP 404; https://dchub.cloud/press-release/2026-08-30-neso-queue-600-gw-uk-delay…` — was `inv-100405-6-of-8-published-story-link-s-are-dead-observ.md` (filed 2026-09-01)
- `https://dchub.cloud/press-release/2026-09-01-spp-oklahoma-dual-markets-77-excess -> HTTP 404; https://dchub.cloud/press-release/35b-anthropic-deal-reprices-powe…` — was `inv-100443-6-of-8-published-story-link-s-are-dead-observ.md` (filed 2026-09-02)
- `https://dchub.cloud/press-release/2026-09-03-meta-499m-spp-four-markets-excess-power -> HTTP 404; https://dchub.cloud/press-release/2026-09-01-spp-oklahoma-dual…` — was `inv-100513-3-of-8-published-story-link-s-are-dead-observ.md` (filed 2026-09-04)
- `https://dchub.cloud/press-release/2026-09-04-spp-five-top-ten-markets -> HTTP 404; https://dchub.cloud/press-release/2026-09-03-meta-499m-spp-four-markets-exces…` — was `inv-100534-3-of-8-published-story-link-s-are-dead-observ.md` (filed 2026-09-05)
- `https://dchub.cloud/press-release/2026-09-05-upper-peninsula-michigan-dcpi-top-five -> HTTP 404; https://dchub.cloud/press-release/2026-09-04-spp-five-top-ten-m…` — was `inv-100548-3-of-8-published-story-link-s-are-dead-observ.md` (filed 2026-09-05)
- `https://dchub.cloud/press-release/20k-facilities-index-live-map-2026-09-06 -> HTTP 404; https://dchub.cloud/press-release/2026-09-05-upper-peninsula-michigan-dc…` — was `inv-100550-4-of-8-published-story-link-s-are-dead-observ.md` (filed 2026-09-07)
- `https://dchub.cloud/press-release/midland-odessa-85-ercot-west-texas-top-build-market-2026-09-06 -> HTTP 404; https://dchub.cloud/press-release/20k-facilities-i…` — was `inv-100552-5-of-8-published-story-link-s-are-dead-observ.md` (filed 2026-09-07)
- `https://dchub.cloud/press-release/auto-2026-09-07-anthropic-517m-14800mw-deal -> HTTP 404; https://dchub.cloud/press-release/2026-09-07-live-facility-index-2070…` — was `inv-100566-7-of-8-published-story-link-s-are-dead-observ.md` (filed 2026-09-07)
- `https://dchub.cloud/press-release/2026-09-08-spp-commands-five-top-eight-excess-power-markets -> HTTP 404; https://dchub.cloud/press-release/ercot-438gw-queue-m…` — was `inv-100593-8-of-8-published-story-link-s-are-dead-observ.md` (filed 2026-09-09)
- `https://dchub.cloud/press-release/2026-09-09-google-hyperscale-deals-29b-capital-power-ready-assets -> HTTP 404; https://dchub.cloud/press-release/spp-oklahoma-…` — was `inv-100596-8-of-8-published-story-link-s-are-dead-observ.md` (filed 2026-09-10)

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
