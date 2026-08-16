<!-- fingerprint:11a3a8638fca32332d1dfcce9b3429eb -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — 1 contradiction(s) on the operator brief surface — observed from the anon seat on contract: /api/v1/operators/equinix reports 543 facilities but /api/v1/operator-brief/equinix returns 'operator_not_found' What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100036). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-07T03:21:20.927466Z · inv #100036_

## The approved recommendation

Choose the fix layer: (a) repair the shared operator-resolution/slug-normalization step in the /operators/*/brief route so all five failing operators resolve (recommended), (b) patch only the Equinix entry in the operator registry (quick but leaves digital-realty/qts/vantage/aligned broken), or (c) declare the brief pages deprecated and update the sentinel manifest. Also decide whether to add a contract-healer invariant asserting that any operator resolvable via /api/v1/operators/<slug> must also resolve via /api/v1/operator-brief/<slug>.

## Triage — 2026-08-16 (spec-debt sweep #2) — CLOSED, exact re-file

Same condition as `docs/brain-proposals/inv-100024-1-contradiction-s-on-the-operator-brief-surface.md`, filed earlier and STILL OPEN as
the canonical obligation. The two differ only in the live counts embedded in
the title — identical once numbers are stripped. Closing this copy does not
close the obligation: act on inv-100024-1-contradiction-s-on-the-operator-brief-surface.md.

## Human checklist

- [x] Confirm this is still worth doing — the CONDITION is still open — this COPY is not; canonical doc is inv-100024-1-contradiction-s-on-the-operator-brief-surface.md
- [x] Scope it to a concrete change (file(s) + approach) — scoping belongs to inv-100024-1-contradiction-s-on-the-operator-brief-surface.md, which stays open
- [x] Implement + verify — not applicable to a duplicate — implement against inv-100024-1-contradiction-s-on-the-operator-brief-surface.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-16 as an exact re-file of inv-100024-1-contradiction-s-on-the-operator-brief-surface.md (spec-debt sweep #2)