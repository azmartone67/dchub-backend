<!-- fingerprint:fe2a41096ee9a89bce3ed547dc76cc7b -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — required_env_var_missing (observed at: env://LINKEDIN_ACCESS_TOKEN). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100217). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-18T19:13:24.154424Z · inv #100217_

## The approved recommendation

Decide whether to regenerate the LinkedIn OAuth token now (LinkedIn tokens require a manual OAuth flow), set LINKEDIN_ACCESS_TOKEN in Railway → service → variables, redeploy, and then trigger the publish-now drain endpoint — versus deliberately leaving LinkedIn distribution disabled. Also decide whether to fund a small follow-up: a startup-time required-env-vars check plus a documented secrets manifest so this class of silent failure alerts at deploy time instead of via queue backlog. No mechanical repo edit is available for the brain to propose.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
