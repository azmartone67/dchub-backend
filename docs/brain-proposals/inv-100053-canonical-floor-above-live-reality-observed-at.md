<!-- fingerprint:0cfe26331b3755779166e8289f08693f -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — canonical_floor_above_live_reality (observed at: ai_surface_canon.PINNED.public). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100053). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-09T07:52:34.892138Z · inv #100053_

## The approved recommendation

Decide which verified-fleet definition is canon post-issue-#1539 (17,096 fleet-filter vs 17,340), then choose one of: (a) re-pin the PINNED.public floor at/below the canonical live verified count, (b) extend the #1346 read-at-source fix to cover PINNED.public, or (c) declare the pin intentionally frozen and suppress/retune the detector. No mechanical find-and-replace is proposed because the pinned value's file and exact text are not in the evidence.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
