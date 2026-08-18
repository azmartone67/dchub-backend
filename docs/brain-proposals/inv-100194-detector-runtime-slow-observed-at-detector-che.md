<!-- fingerprint:ed14b47fa1700d195466a29ac9a7a4c4 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — detector_runtime_slow (observed at: detector:check_llms_txt_contract). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100194). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-18T08:15:35.396543Z · inv #100194_

## The approved recommendation

Choose whether to (a) close this finding as likely-covered by the 2026-07-03/2026-07-06 detector_runtime_slow fixes after a single re-run of check_llms_txt_contract confirms runtime is under threshold, or (b) commission a profiling pass (per-probe wall-clock timing + source review of the detector) to identify the actual bottleneck before any code change. No mechanical remedy is proposed because no source code or unique find string is available in the evidence.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
