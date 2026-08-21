<!-- fingerprint:e6f799abf2bbecc0f1f01753db9a001d -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — facility_duplicates_unmarked (observed at: /api/v1/admin/facility-dedup/analyze?country=GB). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100307). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-21T04:23:03.147602Z · inv #100307_

## The approved recommendation

Decide whether to (a) execute POST /api/v1/admin/facility-dedup/apply?country=GB&confirm=1 now to collapse the 15 GB duplicates (and optionally BR and SG which show the same backlog), and (b) whether to schedule the dedup apply pass as a recurring job so cross-source arrivals stop accumulating as unmarked duplicates. No code change is proposed. No mechanical remedy block applies because the fix is an ops action, not a text edit, and the only code snippet available is a window whose find-string uniqueness cannot be verified.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
