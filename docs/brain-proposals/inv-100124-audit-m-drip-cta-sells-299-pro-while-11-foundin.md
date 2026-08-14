<!-- fingerprint:ae53e0844b77318af55e5ade5e213790 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — audit_M Drip CTA sells $299 Pro while 11 Founding licenses at $99 — the only proven converter — sit unsold (observed at: dchub://audit/SH52-109). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100124). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-14T06:04:46.801938Z · inv #100124_

## The approved recommendation

Decide whether to (a) authorize an evidence pull (fetch dchub://audit/SH52-109, locate the drip CTA source file, and query per-tier conversion counts) before any change, or (b) treat Founding-vs-Pro CTA priority as a deliberate pricing strategy question and rule on it directly. No mechanical remedy block is emitted because no file content or verified-unique find string exists in the evidence — proposing one would violate the no-guessing rule.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
