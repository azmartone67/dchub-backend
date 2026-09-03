<!-- fingerprint:f0806dec5dfd88bf408adc023f21bb91 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — operator_profile_gap:Equinix (observed at: /operators/equinix). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100458). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-03T08:56:30.610540Z · inv #100458_

## The approved recommendation

Open a data-enrichment/normalization ticket in dchub-backend to (1) add 'Equinix, Inc.' -> 'Equinix' to the canonical provider-name normalization map so the two profiles merge, and (2) queue discovery to populate power_mw and market for the merged Equinix facilities; do NOT ship a route-file find-and-replace because no unique mechanical string fix exists.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
