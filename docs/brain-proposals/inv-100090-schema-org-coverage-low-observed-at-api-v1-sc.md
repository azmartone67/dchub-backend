<!-- fingerprint:0ecf2957f174c643afd9d77cade6429c -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — schema_org_coverage_low (observed at: /api/v1/schema-org/missing). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100090). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-11T00:09:55.953396Z · inv #100090_

## The approved recommendation

Decide whether to (a) manually fetch /api/v1/schema-org/missing with a key to get the current page-level worklist and hand the JSON-LD additions to content work, and (b) exempt or throttle-adjust the autopilot's schema-org remediation job so it stops hitting rate_limited and reopening — versus continuing to let the automated loop retry. No code change is proposed until the handler source and current worklist are inspected.

## Triage — 2026-08-17 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/agenda-88-reliability-brain-finding-schema-org-coverage.md`, which stays
OPEN as the single obligation for `schema_org_coverage_low`. This doc's target —
`/api/v1/schema-org/missing` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on agenda-88-reliability-brain-finding-schema-org-coverage.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is agenda-88-reliability-brain-finding-schema-org-coverage.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in agenda-88-reliability-brain-finding-schema-org-coverage.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against agenda-88-reliability-brain-finding-schema-org-coverage.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-17 as a class member of agenda-88-reliability-brain-finding-schema-org-coverage.md (class collapse)