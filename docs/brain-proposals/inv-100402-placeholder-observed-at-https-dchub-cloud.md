<!-- fingerprint:c2751dd4a1d768b22770aa9d4a02b01d -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — — placeholder (observed at: https://dchub.cloud/grid/ERCOT). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100402). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-31T19:52:20.137487Z · inv #100402_

## The approved recommendation

Approve a diagnostic pass (curl the live /grid/ERCOT page + the three failing grid API endpoints, then grep the repo for the captured placeholder string) before any code change — and decide whether the fix should target the backend routes, the edge routing (_routes.json/_worker.js), or the page template once the failing layer is confirmed. No remedy block is emitted because no verified, unique find string from a grid-related file exists in the evidence.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
