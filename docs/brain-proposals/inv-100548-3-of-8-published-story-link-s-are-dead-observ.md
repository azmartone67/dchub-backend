<!-- fingerprint:b93fd7196f6ba7c947f0406d82a16c78 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — 3 of 8 published story link(s) are dead — observed from the none seat on media: https://dchub.cloud/press-release/2026-09-05-upper-peninsula-michigan-dcpi-top-five -> HTTP 404; https://dchub.cloud/press-release/2026-09-04-spp-five-top-ten-markets... What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100548). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-05T23:32:04.405683Z · inv #100548_

## The approved recommendation

In the dchub-frontend repo, run `gh workflow run press-rss.yml` to re-bake press releases, then `curl -i https://dchub.cloud/press-release/2026-09-05-upper-peninsula-michigan-dcpi-top-five` and `curl -i https://dchub.cloud/press-release/2026-09-04-spp-five-top-ten-markets` to confirm both now return HTTP 200.

## Triage — 2026-09-26 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/inv-100383-5-of-8-published-story-link-s-are-dead-observ.md`, which stays
OPEN as the single obligation for `media_story_links_dead`. This doc's target —
`https://dchub.cloud/press-release/2026-09-05-upper-peninsula-michigan-dcpi-top-five -> HTTP 404; https://dchub.cloud/press-release/2026-09-04-spp-five-top-ten-m…` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on inv-100383-5-of-8-published-story-link-s-are-dead-observ.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is inv-100383-5-of-8-published-story-link-s-are-dead-observ.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in inv-100383-5-of-8-published-story-link-s-are-dead-observ.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against inv-100383-5-of-8-published-story-link-s-are-dead-observ.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-09-26 as a class member of inv-100383-5-of-8-published-story-link-s-are-dead-observ.md (class collapse)