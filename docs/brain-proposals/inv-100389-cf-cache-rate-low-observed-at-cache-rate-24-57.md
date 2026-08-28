<!-- fingerprint:0e9270779857a873a0810f27643ee5a9 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — cf_cache_rate_low (observed at: cache_rate:24.57%). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100389). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-28T20:18:13.105289Z · inv #100389_

## The approved recommendation

Approve a small PR adding `Cache-Control: public, max-age=N` (with per-endpoint TTLs you choose, e.g. 300–3600s) to the public GET endpoints named in prior findings (/api/v1/stats, /api/v1/news, /api/v1/grid/totals, /api/v1/dcpi/scores, /.well-known/mcp.json), plus verify Cloudflare cache rules actually cache those API responses — or decide the 24.57% vs 25% gap is too marginal to spend effort on now. No remedy block is emitted because no verified-unique existing string can be mechanically replaced to add absent headers.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
