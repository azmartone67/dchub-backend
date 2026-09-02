<!-- fingerprint:3df715559a7de9fb66031f2b31c14366 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — iso_metric_count_zero_24h (observed at: grid_data: iso=EU_SE_4). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100429). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-02T03:07:17.098920Z · inv #100429_

## The approved recommendation

Inspect the shared EU/ENTSOE grid ingestion workflow and its cron entry '29 */2 * * *' (the flagged cron_schedule_collision) and its most recent run logs to confirm whether the collector job is failing or unscheduled for the whole EU ISO cluster before touching any per-ISO module.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
