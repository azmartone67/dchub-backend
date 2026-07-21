<!-- fingerprint:bd8f0a78bd39aefe3eb60b1136f7a8bb -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: customer_activation_systemic_failure @ /api/v1/admin/customer-white-glove/s

> Auto-captured from an **approved** brain prop item (#100059). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-21T18:10:44.438247Z · prop #100059_

## The approved recommendation

Choose between (A) redefining the customer_activation_systemic_failure detector to count only invoiced payers without keys (accepting the risk the current 1-customer gap undercounts due to join gaps) and closing the 15 recurrences as measurement artifacts, versus (B) keeping the current definition and proceeding with the prior ACTIVATION_NUDGE_ARM recovery campaign aimed at the flagged-but-uninvoiced population. Also decide whether the single confirmed stranded payer gets an immediate manual white-glove touch.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
