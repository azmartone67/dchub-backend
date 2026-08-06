<!-- fingerprint:11a3a8638fca32332d1dfcce9b3429eb -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — 1 contradiction(s) on the operator brief surface — observed from the anon seat on contract: /api/v1/operators/equinix reports 543 facilities but /api/v1/operator-brief/equinix returns 'operator_not_found' What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100024). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-06T03:04:47.957121Z · inv #100024_

## The approved recommendation

Decide between: (a) re-verify /api/v1/operator-brief/equinix now and, if still 404, confirm whether PR #2272's fix is deployed and covers all five brief routes; or (b) authorize a small backend change making the operator-brief handler use the same slug→provider resolver as /api/v1/operators/* (route-family fix, not per-operator). Also decide whether to fold in provider-name normalization (Equinix vs 'Equinix, Inc.') now or defer it as a separate data-quality task.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
