<!-- fingerprint:5315bcae40b655448b633330d93a41cc -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: event_submission_pending:DCD>Connect London 2026 @ /events (seen x22)

> Auto-captured from an **approved** brain agenda item (#100188). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-10T19:31:45.964572Z · agenda #100188_

## The approved recommendation

Two decisions: (1) Approve building the finding-lifecycle state machine (idempotent finding keys + awaiting_human suppression + deadline-escalation with a default action), and choose what the default action is when no human decides in time (auto-decline vs. hold-and-alarm). (2) Separately and urgently: decide now whether DC Hub submits to DCD>Connect London 2026 — this is the pending human decision the 22 recurrences have been asking for, and no architectural fix substitutes for making it.

## Triage — 2026-09-26 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/inv-100044-event-submission-pending-dcd-connect-london-2026.md`, which stays
OPEN as the single obligation for `event_submission_pending`. This doc's target —
`DCD>Connect London 2026 @ /events` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on inv-100044-event-submission-pending-dcd-connect-london-2026.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is inv-100044-event-submission-pending-dcd-connect-london-2026.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in inv-100044-event-submission-pending-dcd-connect-london-2026.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against inv-100044-event-submission-pending-dcd-connect-london-2026.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-09-26 as a class member of inv-100044-event-submission-pending-dcd-connect-london-2026.md (class collapse)