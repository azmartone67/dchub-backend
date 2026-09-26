<!-- fingerprint:e59dfcdc17875b2a997345c5e8a5358c -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — operator_profile_gap:Digital Realty (observed at: /operators/digital-realty). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100460). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-03T08:56:22.501642Z · inv #100460_

## The approved recommendation

Run a discovery/enrichment backfill pass over discovered_facilities rows WHERE provider='Digital Realty' to populate missing power_mw and market fields, then re-run the operator_profile_gap detector on /operators/digital-realty to confirm the 416 count drops.

## Triage — 2026-09-26 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/inv-100410-operator-profile-gap-equinix-inc-observed-at.md`, which stays
OPEN as the single obligation for `operator_profile_gap`. This doc's target —
`Digital Realty @ /operators/digital-realty` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on inv-100410-operator-profile-gap-equinix-inc-observed-at.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is inv-100410-operator-profile-gap-equinix-inc-observed-at.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in inv-100410-operator-profile-gap-equinix-inc-observed-at.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against inv-100410-operator-profile-gap-equinix-inc-observed-at.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-09-26 as a class member of inv-100410-operator-profile-gap-equinix-inc-observed-at.md (class collapse)