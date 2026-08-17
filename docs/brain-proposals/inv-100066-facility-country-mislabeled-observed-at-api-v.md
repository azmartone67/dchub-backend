<!-- fingerprint:61eb798c60eda05338d4983be5d8233a -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — facility_country_mislabeled (observed at: /api/v1/admin/facility-geo/analyze). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100066). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-10T14:37:46.866533Z · inv #100066_

## The approved recommendation

Approve or decline running POST /api/v1/admin/facility-geo/apply?confirm=1 (reversible via /undo) after re-running the analyze endpoint to confirm the current mismatch list — and decide whether to commission a follow-up code investigation into the ingestion path that produced the mislabels and the duplicate '?' country encodings. No mechanical code fix applies: the evidence contains no source file or unique string to patch; the remediation is a data operation through an existing admin endpoint.

## Triage — 2026-08-17 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/agenda-100127-reliability-brain-finding-facility-country-mi.md`, which stays
OPEN as the single obligation for `facility_country_mislabeled`. This doc's target —
`/api/v1/admin/facility-geo/analyze` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on agenda-100127-reliability-brain-finding-facility-country-mi.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is agenda-100127-reliability-brain-finding-facility-country-mi.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in agenda-100127-reliability-brain-finding-facility-country-mi.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against agenda-100127-reliability-brain-finding-facility-country-mi.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-17 as a class member of agenda-100127-reliability-brain-finding-facility-country-mi.md (class collapse)