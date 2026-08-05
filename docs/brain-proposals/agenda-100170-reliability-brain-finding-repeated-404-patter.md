<!-- fingerprint:d2c03c40e632c48bf15e8c7741e9a89a -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: repeated_404_pattern @ /js/dchub-nav.js (seen x196)

> Auto-captured from an **approved** brain agenda item (#100170). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-05T05:17:04.548220Z · agenda #100170_

## The approved recommendation

Choose the fix direction: (a) restore /js/dchub-nav.js to the build (if the nav script is still needed), or (b) remove/replace the stale reference and serve a 410 for the path — and approve adding the CI asset-manifest check as the class-level prevention, versus continuing per-instance patching.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
