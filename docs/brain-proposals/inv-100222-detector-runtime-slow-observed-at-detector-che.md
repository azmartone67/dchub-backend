<!-- fingerprint:33cf6a487feb6ede6c63903ffa09495d -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — detector_runtime_slow (observed at: detector:check_cron_endpoint_unscheduled). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100222). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-18T19:13:06.251775Z · inv #100222_

## The approved recommendation

Choose between (a) commissioning a profiling/tracing pass on the full detector scan loop to find the shared bottleneck across all slow detectors, (b) pulling the source of check_cron_endpoint_unscheduled for a targeted audit (sequential HTTP calls, unbounded queries, missing per-probe timeout), or (c) raising the per-detector threshold / scan budget as a stopgap. No mechanical find-and-replace fix applies because the detector's code was not available in evidence and no unique find string can be verified.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
