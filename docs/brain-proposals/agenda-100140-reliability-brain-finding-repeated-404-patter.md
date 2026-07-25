<!-- fingerprint:5c07406ea072f61d321c3b1efa5bea87 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: repeated_404_pattern @ /api/grid/prices (seen x121)

> Auto-captured from an **approved** brain agenda item (#100140). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-25T08:15:50.903067Z · agenda #100140_

## The approved recommendation

Choose between (A) a family-wide alias/rewrite for /api/grid/* plus a CI route-contract test and detector landing-verification (systemic, recommended), or (B) another per-endpoint patch matching the three prior one-off fixes. If (A), also decide whether unknown external callers should get a permanent alias (200) or a redirect/deprecation response, pending a request-log review to identify who is calling the unversioned paths.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
