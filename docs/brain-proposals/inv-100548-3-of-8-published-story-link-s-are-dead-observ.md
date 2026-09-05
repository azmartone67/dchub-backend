<!-- fingerprint:b93fd7196f6ba7c947f0406d82a16c78 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — 3 of 8 published story link(s) are dead — observed from the none seat on media: https://dchub.cloud/press-release/2026-09-05-upper-peninsula-michigan-dcpi-top-five -> HTTP 404; https://dchub.cloud/press-release/2026-09-04-spp-five-top-ten-markets... What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100548). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-05T23:32:04.405683Z · inv #100548_

## The approved recommendation

In the dchub-frontend repo, run `gh workflow run press-rss.yml` to re-bake press releases, then `curl -i https://dchub.cloud/press-release/2026-09-05-upper-peninsula-michigan-dcpi-top-five` and `curl -i https://dchub.cloud/press-release/2026-09-04-spp-five-top-ten-markets` to confirm both now return HTTP 200.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
