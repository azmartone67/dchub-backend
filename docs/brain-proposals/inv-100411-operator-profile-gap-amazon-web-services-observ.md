<!-- fingerprint:26f91c5d3c1666c53b47f50de92810cb -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — operator_profile_gap:Amazon Web Services (observed at: /operators/amazon-web-services). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100411). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-01T00:13:21.463008Z · inv #100411_

## The approved recommendation

Decide whether to (a) schedule an AWS-prioritized enrichment/verification run (backfill power_mw and market, dedup the 510-tracked vs 203-verified delta) and add operator-name normalization (AWS aliases) to the pipeline, or (b) accept the current profile completeness and tune the operator_profile_gap detector threshold instead. No mechanical one-file fix applies because the root cause is missing/unverified data, not code.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
