<!-- fingerprint:e20b51f8733e45c0412030964eddb0ad -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — linkedin_engagement_readback_stale (observed at: table:linkedin_posts). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100180). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-18T03:00:52.352739Z · inv #100180_

## The approved recommendation

Choose: (a) rotate/regenerate LINKEDIN_ACCESS_TOKEN in Railway with r_organization_social + r_organizational_social_feed scopes and trigger POST /api/linkedin/engagement-sync now (recommended, ops-only), and (b) whether to additionally commission a reviewed code change making fetch_linkedin_engagement isolate per-post 403s instead of aborting the whole batch. No mechanical remedy block is provided because the fix is credential/config work and the handler's exact source text is not in evidence, so no unique find-string can be guaranteed.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
