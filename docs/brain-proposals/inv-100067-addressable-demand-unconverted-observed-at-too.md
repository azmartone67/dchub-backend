<!-- fingerprint:eab91612fe4c2bd738bd26b6302407c2 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — addressable_demand_unconverted (observed at: tool:analyze_site). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100067). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-10T14:37:42.627297Z · inv #100067_

## The approved recommendation

Choose one: (a) permanently bench/re-channel the addressable_demand_unconverted auto-remediation and commit a human to manual outreach on the top analyze_site free callers using the existing bind_email/claim_free_key/unlock_more_data surfaces, or (b) accept the detector noise and deprioritize conversion of this demand. No mechanical code fix is proposed because this is a business-funnel and ops judgement call, not a single-file defect — there is no unique find string to replace, so the remedy block is intentionally omitted.

## Triage — 2026-08-17 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/agenda-100098-reliability-brain-finding-addressable-demand.md`, which stays
OPEN as the single obligation for `addressable_demand_unconverted`. This doc's target —
`tool:analyze_site` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on agenda-100098-reliability-brain-finding-addressable-demand.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is agenda-100098-reliability-brain-finding-addressable-demand.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in agenda-100098-reliability-brain-finding-addressable-demand.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against agenda-100098-reliability-brain-finding-addressable-demand.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-17 as a class member of agenda-100098-reliability-brain-finding-addressable-demand.md (class collapse)