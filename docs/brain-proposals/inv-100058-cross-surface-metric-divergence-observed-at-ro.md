<!-- fingerprint:c94a8795fd825c6221310f4fc351dc90 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — cross_surface_metric_divergence (observed at: routes/competitive_seo.py:210). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100058). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-10T08:51:25.855281Z · inv #100058_

## The approved recommendation

Approve a code change that replaces the hardcoded markets literal in routes/competitive_seo.py:210 (and the sibling at routes/quarterly_report.py:67) with a call to canonical_stats.markets_phrase()/get_canonical_stats(), rather than approving a literal 300→320 swap that will drift again. No fenced remedy block is provided because the exact current file text is unavailable, so a verbatim, provably-unique find string cannot be supplied — this fix requires a human (or an agent with file access) to author the patch.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
