<!-- fingerprint:da2a061df75e2903c529a4eae3299eee -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — detector_runtime_slow (observed at: detector:check_shadowed_routes). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100223). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-18T19:12:59.814591Z · inv #100223_

## The approved recommendation

Decide whether to (a) close the check_shadowed_routes finding as covered by the already-shipped detector_runtime_slow fixes (brain_findings 8353/8439/9802) after confirming its current runtime is under 15s, or (b) commission a profiled re-run of the detector to identify the specific bottleneck before any code change. No mechanical remedy is proposed because the detector's source and a unique find string are not present in the evidence.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
