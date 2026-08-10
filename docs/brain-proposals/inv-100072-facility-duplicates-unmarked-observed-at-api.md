<!-- fingerprint:86ecb77144730bfd29f5892e1b69e863 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — facility_duplicates_unmarked (observed at: /api/v1/admin/facility-dedup/analyze?country=NL). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100072). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-10T14:37:26.311967Z · inv #100072_

## The approved recommendation

Approve running POST /api/v1/admin/facility-dedup/apply?country=NL&confirm=1 (a data-mutating admin action), and decide whether to invest in a recurring/post-ingest dedup pass to stop this class of finding from recurring per-country — versus continuing manual per-country re-runs. No remedy block is emitted because this is an ops/data action with no code shown in evidence, so no unique find-and-replace string can be verified.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
