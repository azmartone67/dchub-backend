<!-- fingerprint:a7826d097455f516ca0a146a7a70d4b1 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — audit_H Known-false press release still live: 'NESO 609 GW = 35% of all US queued capacity' (NESO is the GB operator) (observed at: dchub://audit/SH52-061). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100395). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-30T03:31:23.877848Z · inv #100395_

## The approved recommendation

Choose between (a) immediately unpublishing/redacting the SH52-061 press release pending correction, or (b) commissioning a targeted search (repo grep for '609 GW'/'NESO'/'35%' plus a query against the press/digest content store) to locate and correct the claim in place — and separately, whether to fund a press fact-gate (numeric + operator-scope validation) given this is the second press-quality incident after the ERCOT repetition finding. No remedy block is emitted because no file containing the false text was identified in the evidence, so no find string can be verified as present or unique.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
