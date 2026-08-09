<!-- fingerprint:515bc57f5faabfca629613b88b689fe0 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [developer_ux] Brain finding: ai_surface_drift:connect:stale_value @ https://dchub.cloud/connect (seen x1

> Auto-captured from an **approved** brain agenda item (#100181). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-09T19:46:48.861236Z · agenda #100181_

## The approved recommendation

Approve a structural refactor: (1) create/enforce a single canonical values+pricing module that connect, mcp.json, server-card.json, AGENTS.md, and llms.txt all template from, and (2) complete brain-surface registration with max_age_gate for these surface families — versus continuing to ship per-instance stale-value patches. If approved, decide whether the detector's expected values should also be sourced from that same module so surface and detector can never disagree.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
