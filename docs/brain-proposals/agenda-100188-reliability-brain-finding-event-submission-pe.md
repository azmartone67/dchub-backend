<!-- fingerprint:5315bcae40b655448b633330d93a41cc -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: event_submission_pending:DCD>Connect London 2026 @ /events (seen x22)

> Auto-captured from an **approved** brain agenda item (#100188). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-10T19:31:45.964572Z · agenda #100188_

## The approved recommendation

Two decisions: (1) Approve building the finding-lifecycle state machine (idempotent finding keys + awaiting_human suppression + deadline-escalation with a default action), and choose what the default action is when no human decides in time (auto-decline vs. hold-and-alarm). (2) Separately and urgently: decide now whether DC Hub submits to DCD>Connect London 2026 — this is the pending human decision the 22 recurrences have been asking for, and no architectural fix substitutes for making it.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
