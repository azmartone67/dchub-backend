<!-- fingerprint:1d1fd185effdf7035671b4820a930b75 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — facility_duplicates_unmarked (observed at: /api/v1/admin/facility-dedup/analyze?country=AU). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100142). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-15T07:40:42.046579Z · inv #100142_

## The approved recommendation

Choose one: (1) simply run POST /api/v1/admin/facility-dedup/apply?country=AU&confirm=1 (and BR/US/GB) as a one-off, accepting the finding will re-fire as new rows arrive; or (2) authorize adding the dedup apply pass to the scheduled cron jobs (all countries, recurring) so this finding class is permanently closed. No mechanical one-file text fix applies — this is an ops/scheduling decision.

## Triage — 2026-08-17 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/agenda-100155-reliability-brain-finding-facility-duplicates.md`, which stays
OPEN as the single obligation for `facility_duplicates_unmarked`. This doc's target —
`/api/v1/admin/facility-dedup/analyze?country=AU` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on agenda-100155-reliability-brain-finding-facility-duplicates.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is agenda-100155-reliability-brain-finding-facility-duplicates.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in agenda-100155-reliability-brain-finding-facility-duplicates.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against agenda-100155-reliability-brain-finding-facility-duplicates.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-17 as a class member of agenda-100155-reliability-brain-finding-facility-duplicates.md (class collapse)