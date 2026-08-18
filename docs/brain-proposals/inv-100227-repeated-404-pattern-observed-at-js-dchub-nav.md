<!-- fingerprint:16736ab853087c21c0405d13d70f7972 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — repeated_404_pattern (observed at: /js/dchub-nav.js). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100227). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-18T21:59:02.318740Z · inv #100227_

## The approved recommendation

Decide whether to (a) close this finding as stale after running curl -i https://dchub.cloud/js/dchub-nav.js and confirming a 200/non-404, or (b) if it still 404s, authorize a targeted investigation: grep the frontend templates for the 'dchub-nav.js' reference and inspect the static-file mount to determine whether the fix belongs in the template (client reference), the build/deploy pipeline (missing artifact), or the server static config. No remedy block is emitted because the referencing file, the asset's true location, and a unique find string are all unknown from the evidence provided.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
