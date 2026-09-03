<!-- fingerprint:7058abfdfad3121a1bf5885889ebd1f6 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — audit_H llms-full.txt is three canon generations stale (15,000+ facilities / 1,500+ deals) — never a heal target (observed at: dchub://audit/SH52-028). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100462). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-03T08:56:13.041120Z · inv #100462_

## The approved recommendation

Fetch the raw contents and build provenance of audit_H llms-full.txt via dchub://audit/SH52-028 (and locate its generator) to confirm whether the '15,000+/1,500+' figures are emitted by a build script or hand-authored, then wire the count to derive from canonical_stats the same way mcp.json does in PR #3633.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
