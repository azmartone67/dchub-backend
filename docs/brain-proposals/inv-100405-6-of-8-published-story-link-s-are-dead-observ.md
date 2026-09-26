<!-- fingerprint:352bed53d382028ff9b01c10d03bdf86 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — 6 of 8 published story link(s) are dead — observed from the none seat on media: https://dchub.cloud/press-release/auto-2026-08-31-neso-600gw-queue-timeline -> HTTP 404; https://dchub.cloud/press-release/2026-08-30-neso-queue-600-gw-uk-delay -> HTT... What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100405). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-01T00:13:44.440201Z · inv #100405_

## The approved recommendation

Approve the cheap first move: re-run `gh workflow run press-rss.yml` in dchub-frontend and re-curl all 8 story URLs. Then choose based on the result — (a) if all return 200, close as a recurrence of the stalled press bake and consider adding a fence that blocks link publication until the baked page exists; (b) if any still 404, authorize a targeted investigation of the slug-generation vs bake-filename convention (drafter vs baker) and decide between normalizing slugs at draft time or adding a redirect map for the already-published broken URLs.

## Triage — 2026-09-26 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/inv-100383-5-of-8-published-story-link-s-are-dead-observ.md`, which stays
OPEN as the single obligation for `media_story_links_dead`. This doc's target —
`https://dchub.cloud/press-release/auto-2026-08-31-neso-600gw-queue-timeline -> HTTP 404; https://dchub.cloud/press-release/2026-08-30-neso-queue-600-gw-uk-delay…` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on inv-100383-5-of-8-published-story-link-s-are-dead-observ.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is inv-100383-5-of-8-published-story-link-s-are-dead-observ.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in inv-100383-5-of-8-published-story-link-s-are-dead-observ.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against inv-100383-5-of-8-published-story-link-s-are-dead-observ.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-09-26 as a class member of inv-100383-5-of-8-published-story-link-s-are-dead-observ.md (class collapse)