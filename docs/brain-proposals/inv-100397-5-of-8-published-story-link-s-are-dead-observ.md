<!-- fingerprint:3b66d07538eb1acb09f335d329f89c0c -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — 5 of 8 published story link(s) are dead — observed from the none seat on media: https://dchub.cloud/press-release/2026-08-29-kansas-city-spp-73-excess-power -> HTTP 404; https://dchub.cloud/press-release/auto-2026-08-29-neso-queue-600-gw-time-to-p... What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100397). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-30T21:02:32.976642Z · inv #100397_

## The approved recommendation

Approve running `gh workflow run press-rss.yml` in the dchub-frontend repo now, then re-curl all 8 press URLs. If the 5 links still 404 after a green run, authorize a deeper fix in scripts/bake_press_static.py (per-release page generation / slug alignment) instead of just re-triggering the lane.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
