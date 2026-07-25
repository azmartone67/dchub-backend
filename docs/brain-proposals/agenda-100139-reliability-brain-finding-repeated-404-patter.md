<!-- fingerprint:e9e5800649d004166d7420e9123d6e6c -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: repeated_404_pattern @ /api/v1/infrastructure/transmission (seen x98)

> Auto-captured from an **approved** brain agenda item (#100139). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-25T04:09:39.535901Z · agenda #100139_

## The approved recommendation

Choose between (A) another instance-level patch for /api/v1/infrastructure/transmission (fast, but the class will recur as it did after issues #982/#888/#884), or (B) approving the class-level route-contract fence (route-inventory CI check + deprecation shim + landing-verified finding closure) covering both this endpoint and /api/grid/prices — and if B, authorize pulling the 404 request logs first to confirm whether the transmission endpoint should be created, redirected, or formally retired with a 410.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
