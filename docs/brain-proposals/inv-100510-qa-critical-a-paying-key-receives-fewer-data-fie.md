<!-- fingerprint:cfc0c409c311439b36dc3d18e520c368 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — qa_critical A paying key receives FEWER data fields than an anonymous caller (9 vs 10) (observed at: dchub://qa-superuser/mcp::paid::beats-anon::get_market_intel#5c8a00). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100510). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-04T04:20:27.684724Z · inv #100510_

## The approved recommendation

Pull the actual source of the get_market_intel handler and its tier field-allowlist definition from dchub-mcp-server (server.mjs / the tool's response builder), diff the paid-tier field set against the free/anon field set, and confirm whether this is the residual of the already-shipped rest_endpoint_leakage fix (brain_findings/8351) before opening any new change.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
