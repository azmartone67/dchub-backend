<!-- fingerprint:0d1fe3ff05d0c2caeb45b065a7f63603 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — frontend_endpoint_slow (observed at: /pricing). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100325). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-21T23:34:23.461096Z · inv #100325_

## The approved recommendation

Decide whether to (1) close this as covered by the 2026-07 frontend_endpoint_slow fixes after a re-probe of /pricing and /api/v1/observability/snapshot confirms latency is back under the 5s cap, or (2) commission a proper caching change (stale-while-revalidate origin cache on the snapshot path, per the /api/pipeline pattern) as a reviewed code change. No mechanical find-and-replace fix applies: the only code evidence is a route decorator window with unverified uniqueness, and the bottleneck is not a one-line string swap.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
