<!-- fingerprint:3df715559a7de9fb66031f2b31c14366 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — iso_metric_count_zero_24h (observed at: grid_data: iso=EU_SE_1). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100452). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-02T20:40:07.045794Z · inv #100452_

## The approved recommendation

Open the scheduled workflow/cron that runs the EU ISO ingestion loop and inspect its last-run status and logs for the EU_SE_1 (iso_eu_se_1.py) fetcher over the past 24h to confirm whether the job halted or the upstream ENTSO-E source is returning empty/erroring; that log determines whether this is a re-enable/redeploy or an upstream-credential fix. No remedy block is emitted because the EVIDENCE contains no EU_SE_1 module source, the returned source candidates are unrelated routes, and a fleet-wide zero across ~11 ISOs is inconsistent with a single unique-string typo — so no mechanical single-file find-and-replace can be verified as correct or unique.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
