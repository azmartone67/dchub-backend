<!-- fingerprint:8bae6dcb29bc0d00bdcf46e417f52d16 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — event_submission_pending:DCD>Connect London 2026 (observed at: /events). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100057). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-09T19:46:30.515688Z · inv #100057_

## The approved recommendation

Decide whether DC Hub submits to DCD>Connect London 2026 before the 2026-09-01 deadline or explicitly withdraws/declines — then record that decision to clear the pending finding, following the same closure pattern used for DCD>Connect Virginia 2026 (brain_findings/7830). No mechanical code fix applies: the 'find' string would live in data/ops state, not in a source file, so the remedy block is intentionally omitted.

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