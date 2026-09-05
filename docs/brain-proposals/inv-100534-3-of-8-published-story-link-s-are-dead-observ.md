<!-- fingerprint:5e1acbf28d445c7e4416c95a87af46d8 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — 3 of 8 published story link(s) are dead — observed from the none seat on media: https://dchub.cloud/press-release/2026-09-04-spp-five-top-ten-markets -> HTTP 404; https://dchub.cloud/press-release/2026-09-03-meta-499m-spp-four-markets-excess-power... What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100534). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-05T06:22:48.224708Z · inv #100534_

## The approved recommendation

Run `gh workflow run press-rss.yml` in the dchub-frontend repo to re-bake the press surface, then `curl -i` each of the 8 published story URLs (starting with https://dchub.cloud/press-release/2026-09-04-spp-five-top-ten-markets) to confirm the 3 previously-404 links now return HTTP 200; if any still 404, pull that slug's row from the press_releases table to distinguish a bake-template mismatch from an intentional unpublish.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
