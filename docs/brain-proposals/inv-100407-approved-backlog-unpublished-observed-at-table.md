<!-- fingerprint:4f2ebdc221d9a7cc75dda0f6d0416b56 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — approved_backlog_unpublished (observed at: table:social_media_posts). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100407). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-01T00:13:36.192991Z · inv #100407_

## The approved recommendation

Decide whether to (a) confirm in the X developer portal that the app is attached to a Project and the token is valid (owner action, covers the known root cause), or (b) commission a code-level trace of the publisher drain (SELECT predicate + Railway logs) to rule out an upstream/dedup cause as seen on 2026-08-24. No mechanical fix is proposed because no code defect appears in the evidence and no unique find string was verified.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
