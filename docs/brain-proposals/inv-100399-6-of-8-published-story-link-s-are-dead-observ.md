<!-- fingerprint:145653d06c453d11d888688d589d5ae0 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — 6 of 8 published story link(s) are dead — observed from the none seat on media: https://dchub.cloud/press-release/2026-08-30-neso-queue-600-gw-uk-delay -> HTTP 404; https://dchub.cloud/press-release/2026-08-29-kansas-city-spp-73-excess-power -> HT... What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100399). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-31T09:20:42.849696Z · inv #100399_

## The approved recommendation

Approve (a) an immediate manual re-run of the press bake workflow (`gh workflow run press-rss.yml` in dchub-frontend) and verification-curl of the 6 dead URLs, and (b) whether to open a follow-up task to fix the cron_schedule_collision suppression (355 rate-limits/24h) so the hourly bake lane stops silently stalling — or instead commission a deeper routing/log investigation first if you suspect the 404s are a route mismatch rather than missing baked pages.

## Triage — 2026-09-26 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/inv-100383-5-of-8-published-story-link-s-are-dead-observ.md`, which stays
OPEN as the single obligation for `media_story_links_dead`. This doc's target —
`https://dchub.cloud/press-release/2026-08-30-neso-queue-600-gw-uk-delay -> HTTP 404; https://dchub.cloud/press-release/2026-08-29-kansas-city-spp-73-excess-powe…` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on inv-100383-5-of-8-published-story-link-s-are-dead-observ.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is inv-100383-5-of-8-published-story-link-s-are-dead-observ.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in inv-100383-5-of-8-published-story-link-s-are-dead-observ.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against inv-100383-5-of-8-published-story-link-s-are-dead-observ.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-09-26 as a class member of inv-100383-5-of-8-published-story-link-s-are-dead-observ.md (class collapse)