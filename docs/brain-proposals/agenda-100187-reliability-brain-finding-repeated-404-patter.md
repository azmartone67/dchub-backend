<!-- fingerprint:1a14e3c3c134bf1fb873a8c74dfd0f3a -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: repeated_404_pattern @ /js/dchub-webmcp.js (seen x879)

> Auto-captured from an **approved** brain agenda item (#100187). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-10T14:37:11.922148Z · agenda #100187_

## The approved recommendation

Decide the fate of /js/dchub-webmcp.js: (A) it is a real product asset — add it to the build/deploy manifest and ship it; or (B) it is a dead reference — remove it from all templates/loaders. Then approve (or reject) adding a deploy-gating asset-integrity check that fails any release referencing a static asset that 404s, as the class-level fix for all /js/ 404 findings.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
