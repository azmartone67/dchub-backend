<!-- fingerprint:86100e353998d4767769fdf7d897c0e2 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — cron_schedule_collision (observed at: 11 14 * * 1). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100210). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-18T19:13:46.795997Z · inv #100210_

## The approved recommendation

First, check whether prior fixes brain_findings/8850, 9016, or 9345 already staggered pockets-weekly-digest.yml vs needs-decision-digest.yml. If not, choose which of the two digests keeps the 14:11 Monday slot and what offset the other gets (e.g., 14:23 or a different day), then apply that one-line cron edit manually — it is under .github/, so it is deliberately outside the auto-remedy path. No remedy block is emitted, per the .github/ exclusion and lack of verbatim file text in evidence.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
