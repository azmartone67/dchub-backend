<!-- fingerprint:e23c3bdd9c3184280939a8ec3f333554 -->
# Brain proposal — [reliability] Brain finding: x_publisher_dead @ table:social_media_posts (seen x28)

> Auto-captured from an **approved** brain agenda item (#100110). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-17T20:15:21.658113Z · agenda #100110_

## The approved recommendation

Choose the path: (A) invest one cycle in instrumenting the X publisher (failure-reason capture + landing verification) before fixing, (B) go straight to the most likely fix (X API credential/auth check with expiry alerting) and accept the risk of guessing wrong, or (C) decide the X channel is no longer worth maintaining and retire the detector so the finding stops firing by design.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
