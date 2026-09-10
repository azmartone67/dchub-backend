<!-- fingerprint:3dd147e192ce8a60b060544ee442d598 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — 8 of 8 published story link(s) are dead — observed from the none seat on media: https://dchub.cloud/press-release/2026-09-09-google-hyperscale-deals-29b-capital-power-ready-assets -> HTTP 404; https://dchub.cloud/press-release/spp-oklahoma-tulsa-o... What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100596). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-10T08:06:33.101723Z · inv #100596_

## The approved recommendation

In the dchub-frontend repo run `gh workflow run press-rss.yml`, then curl each of the 8 reported press-release URLs (starting with https://dchub.cloud/press-release/2026-09-09-google-hyperscale-deals-29b-capital-power-ready-assets) and confirm HTTP 200; if any still 404 after the bake completes, escalate to a url_registry redirect check.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
