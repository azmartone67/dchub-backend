<!-- fingerprint:fc5303c635882dd814f949e0503ef382 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — population_shift_unmarked (observed at: week:2026-08-24). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100517). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-04T21:39:42.037320Z · inv #100517_

## The approved recommendation

Add a definition-change marker row for week 2026-08-24 to weekly_series._DEFINITION_CHANGES (effective_at=2026-08-24) recording the CI/GitHub-Actions caller-class exclusion, so deltas spanning that week suppress the false demand-collapse publish; open the weekly_series definitions file directly to insert it rather than editing any of the five unrelated route candidates.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
