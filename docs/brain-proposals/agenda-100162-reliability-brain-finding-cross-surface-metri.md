<!-- fingerprint:bced9d031faf66ea1f0c3368d8c9c9f5 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: cross_surface_metric_divergence @ routes/state_of_power.py:249 (seen x164)

> Auto-captured from an **approved** brain agenda item (#100162). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-03T19:53:33.223123Z · agenda #100162_

## The approved recommendation

Choose between: (A) approve the class-level fix — build the canonical-literal CI fence plus the substance-gated resolution for this fingerprint (more upfront work, stops recurrence), (B) just patch line 249 to call get_canonical_stats() a 4th time (fast, but the 164-count pattern says it will recur elsewhere), or (C) first audit whether the 164 count is detector double-counting before investing in either fix.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
