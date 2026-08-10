<!-- fingerprint:c3c29f4b56229205a9b9151c7ef3c20f -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [developer_ux] Brain finding: cadence_stall_automerge_activity @ /admin/cadence-sentinel#automerge_activi

> Auto-captured from an **approved** brain agenda item (#100184). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-10T08:51:12.758279Z · agenda #100184_

## The approved recommendation

Choose between (a) approving the structural fix — per-cycle heartbeat rows (gate_state, queue_depth, merge/would_merge counts) from the automerge loop plus escalate-with-trace on stall, (b) first commissioning a one-off root-cause investigation of why the loop went silent (scheduler vs gate vs crash-before-Flask) before building anything, or (c) accepting per-instance restarts as the operating model for this lane.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
