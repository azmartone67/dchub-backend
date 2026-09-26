<!-- fingerprint:22a4b59fc59caaef8e1ef903452bc052 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — csp_violation_recurring (observed at: csp://connect-src/dchub.cloud). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100667). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-18T01:00:28.892626Z · inv #100667_

## The approved recommendation

Open dchub-frontend/_headers, confirm whether https://dchub.cloud is present in the connect-src, frame-src, and script-src-elem directives, add any missing, and redeploy the Pages frontend; then confirm the /api/csp-report POST volume drops to zero over 24h.

## Triage — 2026-09-26 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/inv-100362-csp-violation-recurring-observed-at-csp-scri.md`, which stays
OPEN as the single obligation for `csp_violation_recurring`. This doc's target —
`csp://connect-src/dchub.cloud` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on inv-100362-csp-violation-recurring-observed-at-csp-scri.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is inv-100362-csp-violation-recurring-observed-at-csp-scri.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in inv-100362-csp-violation-recurring-observed-at-csp-scri.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against inv-100362-csp-violation-recurring-observed-at-csp-scri.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-09-26 as a class member of inv-100362-csp-violation-recurring-observed-at-csp-scri.md (class collapse)