# Brain proposal — [reliability] Brain finding: trial_to_paid_stagnation @ funnel:signals_to_conversions (seen x3436)

> Auto-captured from an **approved** brain agenda item (#85). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-11T18:54:58.880472Z · agenda #85_

## The approved recommendation

Approve converting trial_to_paid_stagnation from a per-cycle re-firing alert into a single stateful open finding with an explicit close criterion (e.g., cleaned trial→paid rate above a threshold you set), computed on an invoice-verified + probe-excluded denominator, with remediation auto-wired to the existing bind_email/_bind nudges — and authorize fixing the /admin/funnel-health sentinel first so resolution can actually be verified. Alternative: keep the current detector and continue per-instance triage.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
