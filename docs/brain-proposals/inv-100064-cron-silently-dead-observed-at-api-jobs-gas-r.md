<!-- fingerprint:648d38ba74c8f974d4433adc7255f6d3 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — cron_silently_dead (observed at: /api/jobs/gas-refresh). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100064). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-10T09:17:47.203959Z · inv #100064_

## The approved recommendation

Decide whether gas-refresh (and its co-dead sibling site-baseline) should be REVIVED — restart/re-enable the scheduler on Railway and verify a manual keyed POST to /api/jobs/gas-refresh returns 200 — or RETIRED, in which case add 'gas-refresh' (and 'site-baseline' if also retired) to _INTENTIONAL_STALE_CRONS. Also decide whether to open a follow-up investigation into why multiple crons died simultaneously (scheduler container health), since repeated per-cron remediation has not stuck per the inspector's 'cron hygiene slipping' findings. No mechanical remedy block is proposed because no verifiable find-string exists in the evidence and the fix is ops/config.

## Triage — 2026-08-17 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/agenda-100131-reliability-brain-finding-cron-silently-dead.md`, which stays
OPEN as the single obligation for `cron_silently_dead`. This doc's target —
`/api/jobs/gas-refresh` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on agenda-100131-reliability-brain-finding-cron-silently-dead.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is agenda-100131-reliability-brain-finding-cron-silently-dead.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in agenda-100131-reliability-brain-finding-cron-silently-dead.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against agenda-100131-reliability-brain-finding-cron-silently-dead.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-17 as a class member of agenda-100131-reliability-brain-finding-cron-silently-dead.md (class collapse)