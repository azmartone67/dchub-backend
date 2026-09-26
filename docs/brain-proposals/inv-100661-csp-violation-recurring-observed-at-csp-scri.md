<!-- fingerprint:adeb33603ebb87fa6e1de6363d68a496 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — csp_violation_recurring (observed at: csp://script-src-elem/dchub.cloud). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100661). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-18T01:02:18.420297Z · inv #100661_

## The approved recommendation

Grep dchub-frontend/_headers for the script-src-elem directive and confirm whether https://dchub.cloud is already listed; if absent, add https://dchub.cloud to that directive and redeploy, then verify no new /api/csp-report POSTs for gating.js appear.

## Triage — 2026-09-26 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/inv-100362-csp-violation-recurring-observed-at-csp-scri.md`, which stays
OPEN as the single obligation for `csp_violation_recurring`. This doc's target —
`csp://script-src-elem/dchub.cloud` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on inv-100362-csp-violation-recurring-observed-at-csp-scri.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is inv-100362-csp-violation-recurring-observed-at-csp-scri.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in inv-100362-csp-violation-recurring-observed-at-csp-scri.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against inv-100362-csp-violation-recurring-observed-at-csp-scri.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-09-26 as a class member of inv-100362-csp-violation-recurring-observed-at-csp-scri.md (class collapse)