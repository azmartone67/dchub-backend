<!-- fingerprint:79f2acb532be1bb094a978eca3d70b43 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — consistency_radar_detector_crashed:check_auto_trial_conversion_rate (observed at: check_auto_trial_conversion_rate). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100228). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-18T21:59:00.031459Z · inv #100228_

## The approved recommendation

Approve pulling the check_auto_trial_conversion_rate detector source and its crash stack trace to confirm the '_conn' NameError, then decide whether to ship the one-line variable fix yourself or task an engineer — and first verify whether the 2026-07-17/18 prior fixes for this detector class already resolved it (in which case only clearing the stale finding is needed). No mechanical remedy is proposed here because the file path and exact find string cannot be verified from the available evidence.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
