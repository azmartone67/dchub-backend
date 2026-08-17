<!-- fingerprint:9b154e7740f02cec1971064d24e2c705 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: repeated_404_pattern @ /api/grid/demand (seen x78)

> Auto-captured from an **approved** brain agenda item (#100138). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-25T04:09:42.994818Z · agenda #100138_

## The approved recommendation

Choose between (A) approving a one-time generalized gateway route-alias/tombstone mechanism (structural, supersedes per-route patching) plus registering /grid/* as brain surfaces, or (B) shipping a fourth per-route patch for /api/grid/demand only. If (A), also decide whether to pull request logs first to identify the caller before implementation.

## Rolled-up targets — class `repeated_404_pattern` (class collapse, 2026-08-17)

This doc is now the single obligation for **7 occurrences** of
`repeated_404_pattern`. The other 6 were closed against it. They are listed here
in full so the collapse loses no target — fixing the class means fixing
every line below, and a fix that only covers this doc's own target has not
discharged the obligation.

- `/api/grid/demand` — was `agenda-100138-reliability-brain-finding-repeated-404-patter.md` (filed 2026-07-25)
- `/api/v1/infrastructure/transmission` — was `agenda-100139-reliability-brain-finding-repeated-404-patter.md` (filed 2026-07-25)
- `/api/grid/prices` — was `agenda-100140-reliability-brain-finding-repeated-404-patter.md` (filed 2026-07-25)
- `/api/v1/energy/naturalgas/price` — was `agenda-100141-reliability-brain-finding-repeated-404-patter.md` (filed 2026-07-26)
- `/api/v1/energy/retail/rates` — was `agenda-100143-reliability-brain-finding-repeated-404-patter.md` (filed 2026-07-26)
- `/api/v1/infrastructure/transmission` — was `inv-100020-brain-finding-repeated-404-pattern-api-v1-in.md` (filed 2026-07-28)
- `/js/dchub-nav.js` — was `agenda-100170-reliability-brain-finding-repeated-404-patter.md` (filed 2026-08-05)

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
