<!-- fingerprint:15be6b129cfd85802c6f661218becdef -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — — placeholder (observed at: https://dchub.cloud/grid/CAISO). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100632). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-12T10:56:18.870445Z · inv #100632_

## The approved recommendation

Capture the live state of https://dchub.cloud/grid/CAISO with `curl -i https://dchub.cloud/grid/CAISO` and `curl -i https://dchub.cloud/api/grid/prices?iso=CAISO`, and paste the exact status codes and any error text back into this investigation to replace the placeholder question with a concrete observed anomaly before any code change is proposed.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
