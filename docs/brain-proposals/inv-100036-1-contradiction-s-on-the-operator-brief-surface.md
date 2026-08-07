<!-- fingerprint:11a3a8638fca32332d1dfcce9b3429eb -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — 1 contradiction(s) on the operator brief surface — observed from the anon seat on contract: /api/v1/operators/equinix reports 543 facilities but /api/v1/operator-brief/equinix returns 'operator_not_found' What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100036). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-07T03:21:20.927466Z · inv #100036_

## The approved recommendation

Choose the fix layer: (a) repair the shared operator-resolution/slug-normalization step in the /operators/*/brief route so all five failing operators resolve (recommended), (b) patch only the Equinix entry in the operator registry (quick but leaves digital-realty/qts/vantage/aligned broken), or (c) declare the brief pages deprecated and update the sentinel manifest. Also decide whether to add a contract-healer invariant asserting that any operator resolvable via /api/v1/operators/<slug> must also resolve via /api/v1/operator-brief/<slug>.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
