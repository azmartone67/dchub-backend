<!-- fingerprint:ab32d03bbb5d426dedc7680bc396db8e -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — auto_trial_signup_rate_low (observed at: auto_trial_keys: minted 8-37d ago). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100359). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-23T22:47:45.735669Z · inv #100359_

## The approved recommendation

Choose between: (a) wait and re-measure the signup rate on the cohort of auto_trial keys minted strictly after the 2026-07-15 #1612 fix before any new change (recommended), or (b) authorize a targeted investigation pulling auto_trial.py and the check_mcp_conversion_stale detector source plus key lifecycle logs to confirm/refute the miscounting hypothesis. No mechanical find-and-replace fix is proposed because no auto_trial flow code appears in the evidence and no verifiably unique find string exists.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
