<!-- fingerprint:ef0c55ada9875494d00d9315e8e99e8a -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — enterprise_bot_present (observed at: mcp_tool_calls: ip_hash=ca46aa0b5d1a). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100555). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-07T06:29:25.409108Z · inv #100555_

## The approved recommendation

Look up ip_hash=ca46aa0b5d1a in /api/v1/bots/whales, confirm it is the patestautomation-mcp-listability-probe crawler, and classify it as scraper-vs-prospect; if scraper, add its UA/ip_hash to the MCP gateway rate-limit/block list rather than changing any route file.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
