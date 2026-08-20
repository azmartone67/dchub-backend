<!-- fingerprint:101840b6b7d72cac2db1063cf22a4b7c -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — ai_platform_crawl_drop:chatgpt (observed at: ai_requests). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100274). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-20T01:56:39.461610Z · inv #100274_

## The approved recommendation

Choose whether to (a) close this alert as covered by the 2026-07-15 prior fix after verifying chatgpt request volume has recovered in ai_requests, or (b) open an ops investigation into CDN/WAF bot-management rules and robots/llms.txt render paths as a shared root cause for the chatgpt+copilot+gemini drops. No code remedy is proposed because no chatgpt-specific code path appeared in evidence and no unique find-string can be verified — this is a config/ops diagnosis, not a mechanical fix.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
