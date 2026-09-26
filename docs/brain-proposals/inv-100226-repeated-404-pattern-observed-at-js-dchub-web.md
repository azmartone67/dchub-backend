<!-- fingerprint:b2314e95dfeba0176340aa1caddf3603 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — repeated_404_pattern (observed at: /js/dchub-webmcp.js). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100226). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-18T21:59:04.411583Z · inv #100226_

## The approved recommendation

Decide whether to (a) run the verification (curl the URL, grep the frontend/templates repo for 'dchub-webmcp.js') and only then authorize a fix, or (b) close this finding as stale given it no longer appears in the live detector worklist. No mechanical remedy is proposed because the referencing file and static-asset layout are not in evidence, so no verbatim-unique find string can be guaranteed.

## Triage — 2026-09-26 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/agenda-100138-reliability-brain-finding-repeated-404-patter.md`, which stays
OPEN as the single obligation for `repeated_404_pattern`. This doc's target —
`/js/dchub-webmcp.js` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on agenda-100138-reliability-brain-finding-repeated-404-patter.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is agenda-100138-reliability-brain-finding-repeated-404-patter.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in agenda-100138-reliability-brain-finding-repeated-404-patter.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against agenda-100138-reliability-brain-finding-repeated-404-patter.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-09-26 as a class member of agenda-100138-reliability-brain-finding-repeated-404-patter.md (class collapse)