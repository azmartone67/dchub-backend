<!-- fingerprint:47f99ed1b68cc77e833211f1ae0fe7ab -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — 4 of 8 published story link(s) are dead — observed from the none seat on media: https://dchub.cloud/press-release/20k-facilities-index-live-map-2026-09-06 -> HTTP 404; https://dchub.cloud/press-release/2026-09-05-upper-peninsula-michigan-dcpi-top-... What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100550). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-07T01:07:40.306332Z · inv #100550_

## The approved recommendation

In the dchub-frontend repo run `gh workflow run press-rss.yml` to re-bake the per-story press-release pages, then curl the 4 reported slugs (starting with /press-release/20k-facilities-index-live-map-2026-09-06) and confirm HTTP 200; if any still 404, verify the corresponding press_releases row exists via /api/press-releases/list before authoring content.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
