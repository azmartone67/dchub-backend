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

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
