<!-- fingerprint:0b53ae5d2f7bec7585843974c2710061 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — audit_H OSM crawl fetched ZERO POIs for 9 consecutive runs; the 08-01-diagnosed masking (no exit(1) + deadman-watch overwri... (observed at: dchub://audit/SH52-002). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100091). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-11T00:09:52.967144Z · inv #100091_

## The approved recommendation

Choose whether to (a) authorize an evidence pull (crawler source, deadman-watch writer, logs for the 9 runs, and the SH52-002 audit artifact) so a precise patch can be drafted, or (b) accept the throttling hypothesis and tune Overpass backoff/bbox caps first, treating the masking fix as a separate follow-up. No mechanical remedy block is proposed because the target file and find string cannot be verified from available evidence.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
