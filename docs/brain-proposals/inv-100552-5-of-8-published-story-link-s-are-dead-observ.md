<!-- fingerprint:c3abb20b3b705fd02ea05e10badf13c2 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — 5 of 8 published story link(s) are dead — observed from the none seat on media: https://dchub.cloud/press-release/midland-odessa-85-ercot-west-texas-top-build-market-2026-09-06 -> HTTP 404; https://dchub.cloud/press-release/20k-facilities-index-li... What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100552). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-07T06:29:37.610763Z · inv #100552_

## The approved recommendation

In the dchub-frontend repo run `gh workflow run press-rss.yml`, wait for it to complete, then curl -i the 5 failing /press-release/ URLs (starting with midland-odessa-85-ercot-west-texas-top-build-market-2026-09-06) to verify they now return HTTP 200.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
