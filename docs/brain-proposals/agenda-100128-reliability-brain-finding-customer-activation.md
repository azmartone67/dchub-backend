<!-- fingerprint:bd8f0a78bd39aefe3eb60b1136f7a8bb -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: customer_activation_systemic_failure @ /api/v1/admin/customer-white-glove/s

> Auto-captured from an **approved** brain agenda item (#100128). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-22T22:58:13.797031Z · agenda #100128_

## The approved recommendation

Choose: (A) accept reclassification — patch the detector to key off invoices_paid_count + key possession (making the finding resolve to the true gap of 1 customer) and manually touch that one payer, or (B) reject reclassification and treat the 30 no-invoice paid-plan flags as a real prospect-activation cohort worth a nudge campaign — in which case rename/split the detector so it stops masquerading as a paying-customer failure.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
