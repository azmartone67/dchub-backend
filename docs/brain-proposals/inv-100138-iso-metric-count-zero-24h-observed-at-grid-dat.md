<!-- fingerprint:e2f4a9d4c60769922bfac54832588da9 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — iso_metric_count_zero_24h (observed at: grid_data: iso=LGEE). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100138). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-15T07:40:55.102832Z · inv #100138_

## The approved recommendation

Decide whether to (a) pull the LGEE collector workflow's last-run logs to confirm root cause (upstream 4xx vs code error vs schedule stall), then (b) restart the collector and authorize a 24h grid_data backfill for iso=LGEE, or (c) defer if the 2026-07-12 fix is confirmed to already cover LGEE and the finding is stale. No code change is recommended until logs identify the failure mode — no mechanical fix is proposed because no verbatim source text is available to edit safely.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
