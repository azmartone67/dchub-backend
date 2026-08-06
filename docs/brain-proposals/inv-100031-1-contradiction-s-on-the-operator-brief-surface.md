<!-- fingerprint:11a3a8638fca32332d1dfcce9b3429eb -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — 1 contradiction(s) on the operator brief surface — observed from the anon seat on contract: /api/v1/operators/equinix reports 543 facilities but /api/v1/operator-brief/equinix returns 'operator_not_found' What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100031). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-06T22:38:07.801183Z · inv #100031_

## The approved recommendation

Choose between: (A) fix the shared /operators/*/brief operator-resolution logic (normalize slug→provider lookup or reuse the /operators endpoint's resolver) — recommended, since the fault pattern spans multiple operators; (B) declare the brief pages intentionally retired and update the site-sentinel manifest instead; or (C) commission a per-operator brief index/materialized view rebuild if inspection shows the briefs read from a stale/missing index. Also decide whether to reproduce the 404 first given the last observed failure was 2026-07-29.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
