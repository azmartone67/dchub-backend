<!-- fingerprint:3ceb9ed1864fe0a59e16627017fad737 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — nav_missing:/markets/ (observed at: https://dchub.cloud/markets/). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100502). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-04T22:39:21.555705Z · inv #100502_

## The approved recommendation

Run `curl -i https://dchub.cloud/js/dchub-nav.js` and, if it returns 404, restore that asset/route first; concurrently open the live-serving /markets template (confirm which of index_api.py vs routes/market_deep_dive.py renders it by checking which returns the 65536-byte body) to add `<script src="/js/dchub-nav.js" defer></script>` before </body> — I will not emit a mechanical remedy block because no verbatim, provably-unique find string was in evidence.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
