<!-- fingerprint:a6f73f2ec9267794173ce0f4113767be -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — 5 of 8 published story link(s) are dead — observed from the none seat on media: https://dchub.cloud/press-release/afternoon-pulse-2026-08-27-upper-peninsula-third -> HTTP 404; https://dchub.cloud/press-release/2026-08-26-cheyenne-wyoming-wecc-powe... What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100387). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-28T20:18:19.286815Z · inv #100387_

## The approved recommendation

Approve the two-step remedy: (1) after a quick reproduce (curl one dead URL + check last press-rss.yml run), trigger `gh workflow run press-rss.yml` in dchub-frontend and confirm all 8 story URLs return 200; (2) decide whether to also fund the small freshness fence (alert when press_releases rows are newer than the newest baked static page) versus accepting recurrence risk. If the reproduce shows the bake is current but 404s persist, redirect the investigation to slug mismatch / edge routing instead.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
