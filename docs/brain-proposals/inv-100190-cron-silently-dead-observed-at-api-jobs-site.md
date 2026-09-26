<!-- fingerprint:445cd743391623c993d2ac18d53c7d58 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — cron_silently_dead (observed at: /api/jobs/site-baseline). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100190). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-18T08:15:47.125914Z · inv #100190_

## The approved recommendation

Choose one: (1) REVIVE — add a site-baseline entry to the JOBS dict in dchub-scheduler.py with an explicit interval you choose, then verify the endpoint returns 200 under the scheduler's auth; or (2) RETIRE — declare it intentionally manual/stale by adding 'site-baseline' to _INTENTIONAL_STALE_CRONS and the path to _CRON_INTENTIONAL_MANUAL so both detectors stop firing. No mechanical fix is proposed because this is a schedule/retirement judgement call and the exact file contents needed for a unique find string are not in evidence.

## Triage — 2026-09-26 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/agenda-100131-reliability-brain-finding-cron-silently-dead.md`, which stays
OPEN as the single obligation for `cron_silently_dead`. This doc's target —
`/api/jobs/site-baseline` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on agenda-100131-reliability-brain-finding-cron-silently-dead.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is agenda-100131-reliability-brain-finding-cron-silently-dead.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in agenda-100131-reliability-brain-finding-cron-silently-dead.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against agenda-100131-reliability-brain-finding-cron-silently-dead.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-09-26 as a class member of agenda-100131-reliability-brain-finding-cron-silently-dead.md (class collapse)