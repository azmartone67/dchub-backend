<!-- fingerprint:6e15b27a91f5e3ab6ffb772c103328fe -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — cron_failing_with_errors: 2/5 recent dcpi_runs have error_count>0 or markets_scored=0 (observed at: dchub://cron/dcpi_recompute). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100273). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-20T01:56:46.502374Z · inv #100273_

## The approved recommendation

Approve a diagnostic pass before any code change: (1) run SELECT started_at, error_count, markets_scored, first error message FROM dcpi_runs ORDER BY started_at DESC LIMIT 5; (2) confirm whether the 2 failing runs are before or after commit 5b1933b85f23 (2026-07-17). Then decide: if errors postdate that commit and share one stack trace, authorize a targeted code fix in routes/dcpi.py; if they predate it, close this alert as already-fixed and let the next runs confirm. No remedy block is emitted because no unique, verified find string exists in evidence and the root cause is not yet measured.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
