<!-- fingerprint:c5fdec94a658b503fc3d65ed8c01b656 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: db_pool_pressure @ /api/v1/brain/db-pool-pressure (value 3)

> Auto-captured from an **approved** brain agenda item (#100245). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-02T20:39:45.783377Z · agenda #100245_

## The approved recommendation

Open brain_findings/8379 and diff its shipped fix against the current auto-publisher and brain-learn-cycle loops, then confirm every DB connection acquisition in those loops is wrapped in try/finally with an explicit release; file the delta as a follow-up PR for any loop still lacking the guarantee.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
