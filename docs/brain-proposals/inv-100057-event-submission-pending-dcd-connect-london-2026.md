<!-- fingerprint:8bae6dcb29bc0d00bdcf46e417f52d16 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — event_submission_pending:DCD>Connect London 2026 (observed at: /events). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100057). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-09T19:46:30.515688Z · inv #100057_

## The approved recommendation

Decide whether DC Hub submits to DCD>Connect London 2026 before the 2026-09-01 deadline or explicitly withdraws/declines — then record that decision to clear the pending finding, following the same closure pattern used for DCD>Connect Virginia 2026 (brain_findings/7830). No mechanical code fix applies: the 'find' string would live in data/ops state, not in a source file, so the remedy block is intentionally omitted.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
