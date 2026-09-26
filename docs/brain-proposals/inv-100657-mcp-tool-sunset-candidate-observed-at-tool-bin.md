<!-- fingerprint:5fe10b1c5c693c1b67890578cf746e55 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — mcp_tool_sunset_candidate (observed at: tool:bind_email). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100657). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-18T01:02:59.750991Z · inv #100657_

## The approved recommendation

Query the mcp_tool_calls table for tool='bind_email' over the last 14 days, joined against server error logs for the bind_email handler, to confirm whether calls arrived-and-errored (real break) versus never-arrived (consumer migration post-PR #4607/#431) before any sunset decision.

## Triage — 2026-09-26 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/inv-100139-mcp-tool-sunset-candidate-observed-at-tool-pla.md`, which stays
OPEN as the single obligation for `mcp_tool_sunset_candidate`. This doc's target —
`tool:bind_email` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on inv-100139-mcp-tool-sunset-candidate-observed-at-tool-pla.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is inv-100139-mcp-tool-sunset-candidate-observed-at-tool-pla.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in inv-100139-mcp-tool-sunset-candidate-observed-at-tool-pla.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against inv-100139-mcp-tool-sunset-candidate-observed-at-tool-pla.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-09-26 as a class member of inv-100139-mcp-tool-sunset-candidate-observed-at-tool-pla.md (class collapse)