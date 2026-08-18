<!-- fingerprint:4e82001d8149f89822623fb656876247 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — audit_H llms.txt self-contradicts on facilities and deals — re-diverged after merged fix #1115 because heal regexes are nou... (observed at: dchub://audit/SH52-027). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100218). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-18T19:13:21.743817Z · inv #100218_

## The approved recommendation

Choose between (A) commissioning the structural fix: make llms.txt facilities/deals figures render from the single canonical_stats query and add a build-time contract test that blocks any artifact carrying two different verified-facility counts, or (B) directing a human engineer to inspect the heal-regex file and #1115 diff directly to confirm whether a narrower patch is safe. No remedy block is emitted because the contradiction is driven by two divergent live data sources (18,373 vs 18,542 verified) and the target file/regex contents were not in evidence — no verbatim, provably-unique find string can be given.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
