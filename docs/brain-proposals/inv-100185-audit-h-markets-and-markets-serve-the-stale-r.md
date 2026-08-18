<!-- fingerprint:eba0c8577a864bc0a0f2a84e8d42ade9 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — audit_H /markets and /markets/ serve the stale Railway 'Market Intelligence' page with a self-de-indexing canonical instead... (observed at: dchub://audit/SH52-072). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100185). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-18T07:10:41.005449Z · inv #100185_

## The approved recommendation

Decide whether to (a) fix at the edge: update the Cloudflare zone-worker allowlist / _routes.json so /markets and /markets/ route to the current origin, and (b) permanently decommission or 301 the stale Railway origin serving the old 'Market Intelligence' page — versus first pulling the actual route/template source to confirm no repo-side handler is the culprit. This is an ops/config change with SEO impact (the self-de-indexing canonical is actively harming indexing), so it needs an owner and a verification step (curl both paths post-change and confirm the canonical).

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
