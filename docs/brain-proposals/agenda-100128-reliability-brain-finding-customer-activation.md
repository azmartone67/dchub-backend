<!-- fingerprint:bd8f0a78bd39aefe3eb60b1136f7a8bb -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: customer_activation_systemic_failure @ /api/v1/admin/customer-white-glove/s

> Auto-captured from an **approved** brain agenda item (#100128). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-22T22:58:13.797031Z · agenda #100128_

## The approved recommendation

Choose: (A) accept reclassification — patch the detector to key off invoices_paid_count + key possession (making the finding resolve to the true gap of 1 customer) and manually touch that one payer, or (B) reject reclassification and treat the 30 no-invoice paid-plan flags as a real prospect-activation cohort worth a nudge campaign — in which case rename/split the detector so it stops masquerading as a paying-customer failure.

## Triage — 2026-08-16 (spec-debt sweep #2) — CLOSED, exact re-file

Same condition as `docs/brain-proposals/prop-100059-reliability-brain-finding-customer-activation.md`, filed earlier and STILL OPEN as
the canonical obligation. The two differ only in the live counts embedded in
the title — identical once numbers are stripped. Closing this copy does not
close the obligation: act on prop-100059-reliability-brain-finding-customer-activation.md.

## Human checklist

- [x] Confirm this is still worth doing — the CONDITION is still open — this COPY is not; canonical doc is prop-100059-reliability-brain-finding-customer-activation.md
- [x] Scope it to a concrete change (file(s) + approach) — scoping belongs to prop-100059-reliability-brain-finding-customer-activation.md, which stays open
- [x] Implement + verify — not applicable to a duplicate — implement against prop-100059-reliability-brain-finding-customer-activation.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-16 as an exact re-file of prop-100059-reliability-brain-finding-customer-activation.md (spec-debt sweep #2)