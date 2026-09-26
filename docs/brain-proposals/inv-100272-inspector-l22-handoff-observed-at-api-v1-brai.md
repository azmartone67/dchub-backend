<!-- fingerprint:1aa24e074cf99c3ab9c7c02003e2d5f8 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — inspector_l22_handoff (observed at: /api/v1/brain/brief/100356/draft-prs). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100272). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-20T01:56:49.478972Z · inv #100272_

## The approved recommendation

Choose between: (a) accept that merged PR #2937 already resolves inspector_l22_handoff and close the finding after observing one successful post-merge inspector cycle where the draft-PR handoff fires for a fresh brief, or (b) if the handoff for brief #100356 failed after #2937 was deployed, commission a targeted investigation of the full handler body in routes/brain_inspector.py (beyond the route-decorator window shown) before any code change. No mechanical find-and-replace fix applies: the route exists, the endpoint returns 200, no defective code was shown in evidence, and the known root-cause fix has already shipped.

## Triage — 2026-09-26 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/agenda-100113-developer-ux-brain-finding-inspector-l22-hand.md`, which stays
OPEN as the single obligation for `inspector_l22_handoff`. This doc's target —
`/api/v1/brain/brief/100356/draft-prs` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on agenda-100113-developer-ux-brain-finding-inspector-l22-hand.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is agenda-100113-developer-ux-brain-finding-inspector-l22-hand.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in agenda-100113-developer-ux-brain-finding-inspector-l22-hand.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against agenda-100113-developer-ux-brain-finding-inspector-l22-hand.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-09-26 as a class member of agenda-100113-developer-ux-brain-finding-inspector-l22-hand.md (class collapse)