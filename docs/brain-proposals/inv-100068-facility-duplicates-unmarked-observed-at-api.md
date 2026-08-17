<!-- fingerprint:556231cd28353c652c2aebd764435592 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — facility_duplicates_unmarked (observed at: /api/v1/admin/facility-dedup/analyze?country=US). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100068). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-10T14:37:42.310609Z · inv #100068_

## The approved recommendation

Choose one: (a) run the one-off POST /api/v1/admin/facility-dedup/apply?country=US&confirm=1 now (after re-verifying the US finding is still live), or (b) approve adding a scheduled post-ingestion dedup-apply pass (per-country or global) so this finding class stops recurring — and if (b), decide whether apply runs fully automated or gated behind a review of the analyze output.

## Triage — 2026-08-17 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/agenda-100155-reliability-brain-finding-facility-duplicates.md`, which stays
OPEN as the single obligation for `facility_duplicates_unmarked`. This doc's target —
`/api/v1/admin/facility-dedup/analyze?country=US` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on agenda-100155-reliability-brain-finding-facility-duplicates.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is agenda-100155-reliability-brain-finding-facility-duplicates.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in agenda-100155-reliability-brain-finding-facility-duplicates.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against agenda-100155-reliability-brain-finding-facility-duplicates.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-17 as a class member of agenda-100155-reliability-brain-finding-facility-duplicates.md (class collapse)