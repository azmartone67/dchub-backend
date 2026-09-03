<!-- fingerprint:5fb88b4f77c96ef487b335189717c656 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — audit_H Detect→propose is the loop's stall point: 55 actionable findings, 0 pending code proposals, 0 open PRs, 0 brain-lan... (observed at: dchub://audit/SH52-040). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100463). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-03T08:56:10.903130Z · inv #100463_

## The approved recommendation

Pull the audit_H Detect→propose transformer source and its recent run logs at dchub://audit/SH52-040 (the finding→proposed persistence handler) and trace why the ≥1 live findings such as operator_profile_gap:Digital Realty (×416) are not being written as pending code proposals — inspect the proposal-creation write path and any confidence/eligibility gate before it.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
