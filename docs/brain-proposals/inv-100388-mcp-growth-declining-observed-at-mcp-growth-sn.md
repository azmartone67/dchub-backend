<!-- fingerprint:5755012eeefa7d24e1e1c2900617fe2d -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — mcp_growth_declining (observed at: mcp_growth_snapshots: latest 2). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100388). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-28T20:18:16.682821Z · inv #100388_

## The approved recommendation

Choose whether to (a) wait one full week for the newly-instrumented (PR #3251/#240) arrival series to accrue and re-baseline mcp_growth_snapshots before acting, or (b) treat the -32.0% decline as organic now and commission an immediate funnel/platform investigation — and separately, approve the ops task to correct the 3 drifted MCP presence listings.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
