<!-- fingerprint:dc25109aa953c3194ae1850927d5ced3 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — data_stale: 'news' — newest row 128.79h old — exceeds SLA 24h (observed at: dchub://data/news). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100134). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-15T07:41:10.055181Z · inv #100134_

## The approved recommendation

Choose the diagnostic path: (1) inspect dchub-scheduler.py JOBS / dchub-jobs config for a missing or disabled news entry, or (2) manually trigger the news refresh endpoint and read the run output under the new PR #2677 failure semantics. No remedy block is emitted because no candidate file contents appear in the evidence, so no find string can be guaranteed verbatim or unique — this is an investigate-first case, not a mechanical fix.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
