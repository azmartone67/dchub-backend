<!-- fingerprint:b832e4ab8ce851be6358a8af9a4767fb -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — 7 of 8 published story link(s) are dead — observed from the none seat on media: https://dchub.cloud/press-release/auto-2026-09-07-anthropic-517m-14800mw-deal -> HTTP 404; https://dchub.cloud/press-release/2026-09-07-live-facility-index-20700-asset... What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100566). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-07T21:11:17.038678Z · inv #100566_

## The approved recommendation

curl -i on all 7 dead press-release URLs (starting with https://dchub.cloud/press-release/auto-2026-09-07-anthropic-517m-14800mw-deal) and capture status + body, then grep the dchub-backend press-release router and sitemap emitter for the 'auto-<date>-' slug pattern to confirm whether PR #4125's canonicalization covers these slugs; file findings against dchub-backend as a follow-up to #4125.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
