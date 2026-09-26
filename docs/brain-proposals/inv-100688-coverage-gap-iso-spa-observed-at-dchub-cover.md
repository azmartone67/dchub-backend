<!-- fingerprint:539385765b62741df63d1d61d7354dff -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — coverage_gap_iso:SPA (observed at: dchub://coverage/iso). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100688). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-20T23:43:29.891399Z · inv #100688_

## The approved recommendation

Grep the dchub-backend repo for the literal string 'SPA' scoped to the coverage/ISO detector (the code path serving dchub://coverage/iso) and paste the matching file+line into the evidence pipe, so a verifiable unique find-string can be produced before any edit.

## Triage — 2026-09-26 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/inv-100528-coverage-gap-iso-wacm-observed-at-dchub-cove.md`, which stays
OPEN as the single obligation for `coverage_gap_iso`. This doc's target —
`SPA @ dchub://coverage/iso` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on inv-100528-coverage-gap-iso-wacm-observed-at-dchub-cove.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is inv-100528-coverage-gap-iso-wacm-observed-at-dchub-cove.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in inv-100528-coverage-gap-iso-wacm-observed-at-dchub-cove.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against inv-100528-coverage-gap-iso-wacm-observed-at-dchub-cove.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-09-26 as a class member of inv-100528-coverage-gap-iso-wacm-observed-at-dchub-cove.md (class collapse)