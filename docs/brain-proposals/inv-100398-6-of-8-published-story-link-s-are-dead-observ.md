<!-- fingerprint:145653d06c453d11d888688d589d5ae0 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — 6 of 8 published story link(s) are dead — observed from the none seat on media: https://dchub.cloud/press-release/2026-08-30-neso-queue-600-gw-uk-delay -> HTTP 404; https://dchub.cloud/press-release/2026-08-29-kansas-city-spp-73-excess-power -> HT... What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100398). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-30T21:02:25.113203Z · inv #100398_

## The approved recommendation

Approve running `gh workflow run press-rss.yml` in dchub-frontend now to re-bake the press pages (then re-check the 6 URLs), and decide whether to open a follow-up task to resolve the cron schedule collisions so the hourly bake lane cannot silently stall again — versus first pulling the press_releases rows and workflow run history to confirm the stalled-bake diagnosis before touching anything.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
