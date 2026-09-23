<!-- fingerprint:bced9d031faf66ea1f0c3368d8c9c9f5 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: cross_surface_metric_divergence @ routes/state_of_power.py:250 (seen x187)

> Auto-captured from an **approved** brain agenda item (#100278). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-23T00:21:42.054351Z · agenda #100278_

## The approved recommendation

Add a CI guard test in the dchub-backend repo that scans routes/*.py (starting with state_of_power.py, quarterly_report.py, competitive_seo.py) and fails the build when a canonical metric (market count, facility count, country count) appears as a numeric literal instead of coming from get_canonical_stats()/canonical_stats.markets_phrase(), and in the same PR replace the remaining hardcoded market literals in routes/state_of_power.py with the canonical accessor.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
