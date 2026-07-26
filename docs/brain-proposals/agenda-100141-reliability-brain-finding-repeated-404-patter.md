<!-- fingerprint:1e9800bb3527d65010c4ba477612cc8c -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: repeated_404_pattern @ /api/v1/energy/naturalgas/price (seen x171)

> Auto-captured from an **approved** brain agenda item (#100141). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-26T05:50:23.432477Z · agenda #100141_

## The approved recommendation

Choose the systemic remedy: (A) fund the route-contract-in-CI fence + deprecation policy (stops the whole 404 finding class), (B) first triage whether /api/jobs/gas-refresh being dead is the root of the energy cluster and revive/remove that subsystem deliberately, or (C) continue per-route alias patches as before (fastest, but the pattern will recur). A curl of https://dchub.cloud/api/v1/energy/naturalgas/price plus a check of who the callers are should precede final commitment.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
