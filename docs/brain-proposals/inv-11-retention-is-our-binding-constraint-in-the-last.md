# Brain proposal — Retention is our binding constraint: in the last 7 days 65 real external AI agents (claude/chatgpt/opencode/gemini/grok platforms in mcp_tool_calls) called the MCP server - up 110% WoW - yet 0 of 65 returned on a second day. Why do first-visit agents never come back, and what is the single highest-leverage product or protocol change to make an agent call DC Hub again tomorrow?

> Auto-captured from an **approved** brain inv item (#11). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-01T23:53:08.352325Z · inv #11_

## The approved recommendation

Choose sequencing: (A) fix the /mcp.json 5xx failures and add stable-identifier instrumentation (map sessions to API key/IP/user-agent) BEFORE any retention feature, then re-measure day-2 return; or (B) proceed now with a retention hook (e.g., freshness footers or watch-style re-check prompts) despite the broken manifest and unverified retention metric. Option A is recommended.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
