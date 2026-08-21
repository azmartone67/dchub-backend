<!-- fingerprint:345571168175c564b75c6cea1eb670a1 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — facility_duplicates_unmarked (observed at: /api/v1/admin/facility-dedup/analyze?country=BR). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100311). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-21T04:22:53.004804Z · inv #100311_

## The approved recommendation

1) Run POST /api/v1/admin/facility-dedup/apply?country=BR&confirm=1 now to collapse the 14 BR duplicates (and decide whether to also run GB's 15). 2) Decide whether to keep dedup-apply as a manual confirm-gated action or schedule it (e.g., post-ingestion cron per country) so this recurring finding class stops reappearing. No code change is proposed.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
