<!-- fingerprint:77432ba15b3cc890c8f41a091f1c15db -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — audit_M Unauthenticated /api/jobs/* requests stamp cron_last_run completion — health signal is spoofable and staleness dete... (observed at: dchub://audit/SH52-115). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100193). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-18T08:15:37.837588Z · inv #100193_

## The approved recommendation

Choose the remediation shape: (a) add a scheduler shared-secret check as a blueprint-level before_request covering all /api/jobs/* routes across the four files, (b) move cron_last_run stamping out of the handlers entirely so only dchub-scheduler.py writes it after a verified 2xx, or (c) both. Also decide whether to first reproduce the unauthenticated stamp against the live endpoint to confirm SH52-115 is still valid after PRs #2848/#2854. No mechanical remedy block is proposed because the fix spans multiple files and no verbatim source text was available to guarantee a unique find string.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
