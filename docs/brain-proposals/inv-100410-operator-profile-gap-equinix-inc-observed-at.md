<!-- fingerprint:159b52db2cde1de0f86fe76129bc60d2 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — operator_profile_gap:Equinix, Inc. (observed at: /operators/equinix,-inc.). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100410). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-01T00:13:27.199779Z · inv #100410_

## The approved recommendation

Decide whether to (a) canonicalize 'Equinix, Inc.' into the 'Equinix' operator identity via a data migration/alias entry (with a 301 redirect from /operators/equinix,-inc.), and (b) whether to then prioritize a power_mw/market backfill for the merged ~674-facility Equinix fleet — versus leaving the two records split and enriching each separately. No mechanical code fix is proposed because the root cause is unnormalized provider data, not a uniquely-identifiable string in a single source file.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
