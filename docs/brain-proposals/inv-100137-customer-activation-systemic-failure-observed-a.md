<!-- fingerprint:153a65b22ef33dbe14feb06197aa0138 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — customer_activation_systemic_failure (observed at: /api/v1/admin/customer-white-glove/state). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100137). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-15T07:40:57.973833Z · inv #100137_

## The approved recommendation

Choose whether to (a) arm ACTIVATION_NUDGE_ARM=1 and personally contact the 1 real payer with no usable key (recommended), and (b) commission a follow-up measurement of calls-past-grace per invoice-paying customer to confirm whether the prior 'stranded' cohort still exists under the stricter definition — versus accepting the current 1-customer gap as the full extent of the problem. No code change is proposed; there is no single mechanical fix because the endpoint shows no errors and the root cause is operational, not a bug in one file.

## Triage — 2026-08-17 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/prop-100059-reliability-brain-finding-customer-activation.md`, which stays
OPEN as the single obligation for `customer_activation_systemic_failure`. This doc's target —
`/api/v1/admin/customer-white-glove/state` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on prop-100059-reliability-brain-finding-customer-activation.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is prop-100059-reliability-brain-finding-customer-activation.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in prop-100059-reliability-brain-finding-customer-activation.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against prop-100059-reliability-brain-finding-customer-activation.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-17 as a class member of prop-100059-reliability-brain-finding-customer-activation.md (class collapse)