<!-- fingerprint:08296abe15238a2ac30209fa60899b04 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: repeated_404_pattern @ /api/v1/energy/retail/rates (seen x164)

> Auto-captured from an **approved** brain agenda item (#100143). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-26T05:50:12.990310Z · agenda #100143_

## The approved recommendation

Choose the direction for the /api/v1/energy/* and /api/grid/* 404 family: (a) implement the endpoints backed by existing grid/energy data (possibly sourcing retail rates), (b) formally decommission them with explicit 410s and fix/notify the callers, or (c) do the caller-origin audit first and defer the build/kill call — and approve adding a route-contract CI check either way.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
