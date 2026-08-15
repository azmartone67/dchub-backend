<!-- fingerprint:20ccd92f2c72161e8f4d41894abb920e -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — mcp_tool_sunset_candidate (observed at: tool:plan_query). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100139). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-15T07:40:52.274776Z · inv #100139_

## The approved recommendation

Choose one: (a) keep plan_query live and open a 14-day watch with per-tool error-log review before revisiting the sunset question, (b) actively deprecate plan_query with a migration notice pointing consumers to execute_plan, or (c) commission the missing codebase/call-site search to determine whether any consumers remain before deciding. No mechanical remedy block is proposed because no file or unique find-string is evidenced and a sunset is a coordinated, judgement-based change.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
