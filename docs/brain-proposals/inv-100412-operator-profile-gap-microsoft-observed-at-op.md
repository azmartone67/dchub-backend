<!-- fingerprint:9b250e1d0b6be0512eba93f23ce2d63d -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — operator_profile_gap:Microsoft (observed at: /operators/microsoft). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100412). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-01T00:13:16.845857Z · inv #100412_

## The approved recommendation

Confirm whether the 2026-07-07 fix (brain_findings/9437) actually closed operator_profile_gap:Microsoft by spot-checking /operators/microsoft, and decide whether to (a) mark this finding resolved, or (b) commission a data-enrichment/backfill pass for the remaining operator profile gaps (Equinix 535, Digital Realty 416, Equinix, Inc. 223) including provider-name normalization. No mechanical code fix applies — the find-and-replace remedy block is intentionally omitted because the root cause is missing facility data, not a string in a source file.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
