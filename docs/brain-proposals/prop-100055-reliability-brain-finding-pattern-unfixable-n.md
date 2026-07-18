<!-- fingerprint:8de9bdc5845671a916177c71ec33a2d9 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: pattern_unfixable_needs_rechannel @ pattern:addressable_demand_unconverted

> Auto-captured from an **approved** brain prop item (#100055). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-18T23:01:08.042725Z · prop #100055_

## The approved recommendation

Approve (or reject) building the re-channel terminal state: (1) which backlog/owner receives benched effect-unfixable patterns, (2) which KPI and threshold closes 'addressable_demand_unconverted' (e.g., paid conversions/30d above the current 12, or paid keys above 37), and (3) whether to suppress re-detection while the ticket is open — plus, separately, who owns the actual demand-conversion decision (pricing/packaging for the 1,548 free callers vs. 37 paid keys), since that is a business call the brain cannot make.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
