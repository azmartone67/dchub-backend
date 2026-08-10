<!-- fingerprint:3d90d029d0a5b5d7b80325203030581e -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — audit_H Canon claim-verification is warn-only on the social path and LinkedIn-only — X/Bluesky posts never get number verif... (observed at: dchub://audit/SH52-063). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100074). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-10T14:37:19.505498Z · inv #100074_

## The approved recommendation

Choose: (a) authorize a code-level re-audit of the social publish/claim-verification path (pulling the actual module) to confirm whether fe197ba23a18 already fixed SH52-063, then approve a scoped PR that removes the LinkedIn-only gate and decides warn-only vs. blocking enforcement for X/Bluesky; or (b) accept warn-only social verification as intended policy and close SH52-063 as by-design. No mechanical remedy is proposed because the exact gating code was not present in the evidence and a verbatim-unique find string cannot be guaranteed.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
