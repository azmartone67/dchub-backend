<!-- fingerprint:aa118b523141580bcab978c0555b0046 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: operator_profile_gap:Digital Realty @ /operators/digital-realty (seen x416)

> Auto-captured from an **approved** brain agenda item (#100260). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-12T10:56:08.005568Z · agenda #100260_

## The approved recommendation

In routes/operators.py (the operator_profile_gap detector path near line 429), refactor the gap detector to emit ONE aggregated finding per normalized operator carrying a completeness percentage (missing power_mw / missing market ratios), and normalize provider strings ('Digital Realty', 'Equinix'/'Equinix, Inc.') before grouping — then backfill Digital Realty's 416 verified facilities' power_mw/market via the discovery-enrichment pipeline.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
