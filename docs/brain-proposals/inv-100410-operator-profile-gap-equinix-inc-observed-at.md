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

## Rolled-up targets — class `operator_profile_gap` (class collapse, 2026-09-26)

This doc is now the single obligation for **6 occurrences** of
`operator_profile_gap`. The other 5 were closed against it. They are listed here
in full so the collapse loses no target — fixing the class means fixing
every line below, and a fix that only covers this doc's own target has not
discharged the obligation.

- `Equinix, Inc. @ /operators/equinix,-inc.` — was `inv-100410-operator-profile-gap-equinix-inc-observed-at.md` (filed 2026-09-01)
- `Equinix @ /operators/equinix` — was `inv-100458-operator-profile-gap-equinix-observed-at-oper.md` (filed 2026-09-03)
- `Digital Realty @ /operators/digital-realty` — was `inv-100460-operator-profile-gap-digital-realty-observed-at.md` (filed 2026-09-03)
- `Equinix, Inc. @ /operators/equinix-inc` — was `inv-100504-operator-profile-gap-equinix-inc-observed-at.md` (filed 2026-09-03)
- `Digital Realty @ /operators/digital-realty` — was `agenda-100260-reliability-brain-finding-operator-profile-ga.md` (filed 2026-09-12)
- `Equinix, Inc. @ /operators/equinix-inc` — was `agenda-100271-reliability-brain-finding-operator-profile-ga.md` (filed 2026-09-19)

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
