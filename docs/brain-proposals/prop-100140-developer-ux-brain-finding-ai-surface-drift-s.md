<!-- fingerprint:1de8da42796b0d7d612bebee86a8c0cf -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [developer_ux] Brain finding: ai_surface_drift:server_card:developer_period @ https://dchub.cloud/.well-k

> Auto-captured from an **approved** brain prop item (#100140). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-20T01:56:44.730368Z · prop #100140_

## The approved recommendation

Choose the systemic path: (a) approve building a single-source-of-truth generator (canon → templated server-card.json/mcp.json/AGENTS.md/connect) plus a pre-deploy CI drift gate, versus (b) extending the existing auto-fixer to patch each field instance as it drifts. Also decide the canonical quota representation (per-day vs per-month) that both the canon and all published surfaces must use.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
