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

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
