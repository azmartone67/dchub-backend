<!-- fingerprint:9b154e7740f02cec1971064d24e2c705 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: repeated_404_pattern @ /api/grid/demand (seen x78)

> Auto-captured from an **approved** brain agenda item (#100138). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-25T04:09:42.994818Z · agenda #100138_

## The approved recommendation

Choose between (A) approving a one-time generalized gateway route-alias/tombstone mechanism (structural, supersedes per-route patching) plus registering /grid/* as brain surfaces, or (B) shipping a fourth per-route patch for /api/grid/demand only. If (A), also decide whether to pull request logs first to identify the caller before implementation.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
