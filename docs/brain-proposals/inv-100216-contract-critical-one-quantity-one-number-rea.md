<!-- fingerprint:d2b7e009fe0132b7c3307108a6c2554a -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — contract_critical: one quantity, one number: real_external_agents_7d (observed at: dchub://contract/B/B.real_external_agents_7d). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100216). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-18T19:13:28.747209Z · inv #100216_

## The approved recommendation

Accept 72 distinct external agents / 2 platforms (post-#202 attribution) as the new honest baseline and update contract B's expected value accordingly — or treat the drop as a real demand regression and commission a deeper investigation into where the pre-#202 'external' traffic actually went. No mechanical code fix is proposed: the attribution fix already shipped in dchub-mcp-server #202, and the calculation file for real_external_agents_7d was not in evidence, so any find/replace string would be a guess.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
