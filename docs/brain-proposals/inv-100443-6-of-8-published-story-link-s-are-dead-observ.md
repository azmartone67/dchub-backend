<!-- fingerprint:8550f7c2a81a309b53debc67b7c79536 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — 6 of 8 published story link(s) are dead — observed from the none seat on media: https://dchub.cloud/press-release/2026-09-01-spp-oklahoma-dual-markets-77-excess -> HTTP 404; https://dchub.cloud/press-release/35b-anthropic-deal-reprices-power-rich-... What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100443). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-02T03:24:01.178526Z · inv #100443_

## The approved recommendation

In dchub-frontend, run `gh workflow run press-rss.yml`, then curl -i the two named URLs (https://dchub.cloud/press-release/2026-09-01-spp-oklahoma-dual-markets-77-excess and the anthropic-deal slug) to confirm they return HTTP 200; if they still 404 after the bake completes, escalate to inspecting scripts/bake_press_static.py slug generation against the press_releases DB rows.

## Triage — 2026-09-26 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/inv-100383-5-of-8-published-story-link-s-are-dead-observ.md`, which stays
OPEN as the single obligation for `media_story_links_dead`. This doc's target —
`https://dchub.cloud/press-release/2026-09-01-spp-oklahoma-dual-markets-77-excess -> HTTP 404; https://dchub.cloud/press-release/35b-anthropic-deal-reprices-powe…` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on inv-100383-5-of-8-published-story-link-s-are-dead-observ.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is inv-100383-5-of-8-published-story-link-s-are-dead-observ.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in inv-100383-5-of-8-published-story-link-s-are-dead-observ.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against inv-100383-5-of-8-published-story-link-s-are-dead-observ.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-09-26 as a class member of inv-100383-5-of-8-published-story-link-s-are-dead-observ.md (class collapse)