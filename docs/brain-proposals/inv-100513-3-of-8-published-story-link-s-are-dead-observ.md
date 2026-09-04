<!-- fingerprint:e150b3d121e0096b1186f31d3a7f8408 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — 3 of 8 published story link(s) are dead — observed from the none seat on media: https://dchub.cloud/press-release/2026-09-03-meta-499m-spp-four-markets-excess-power -> HTTP 404; https://dchub.cloud/press-release/2026-09-01-spp-oklahoma-dual-market... What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100513). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-04T22:42:08.554955Z · inv #100513_

## The approved recommendation

In the dchub-frontend repo run `gh workflow run press-rss.yml`, wait for it to complete, then `curl -i https://dchub.cloud/press-release/2026-09-03-meta-499m-spp-four-markets-excess-power` and the other two dead URLs to confirm they now return HTTP 200; if any still 404, query the press_releases DB table for those exact slugs to determine whether the row exists.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
