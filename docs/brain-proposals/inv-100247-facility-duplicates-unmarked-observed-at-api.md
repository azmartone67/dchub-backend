<!-- fingerprint:86ecb77144730bfd29f5892e1b69e863 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — facility_duplicates_unmarked (observed at: /api/v1/admin/facility-dedup/analyze?country=NL). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100247). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-19T03:52:59.307708Z · inv #100247_

## The approved recommendation

Choose between (a) the tactical fix: run POST /api/v1/admin/facility-dedup/apply?country=NL&confirm=1 (and AU, per the live finding) after re-verifying via the analyze endpoint, or (b) the structural fix: authorize scheduling the dedup apply pass as an automatic post-ingestion job across all countries so this finding class stops refiring. No code change is proposed; no remedy block is emitted because the fix is operational, not mechanical.

## Triage — 2026-09-26 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/agenda-100155-reliability-brain-finding-facility-duplicates.md`, which stays
OPEN as the single obligation for `facility_duplicates_unmarked`. This doc's target —
`/api/v1/admin/facility-dedup/analyze?country=NL` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on agenda-100155-reliability-brain-finding-facility-duplicates.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is agenda-100155-reliability-brain-finding-facility-duplicates.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in agenda-100155-reliability-brain-finding-facility-duplicates.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against agenda-100155-reliability-brain-finding-facility-duplicates.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-09-26 as a class member of agenda-100155-reliability-brain-finding-facility-duplicates.md (class collapse)