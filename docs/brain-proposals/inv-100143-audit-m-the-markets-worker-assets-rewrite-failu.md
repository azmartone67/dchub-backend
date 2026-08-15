<!-- fingerprint:4e900250cd2ebda041e7f3d5ea642f8e -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — audit_M The /markets worker ASSETS-rewrite failure mode is silent — no guard catches a Pages-static page being shadowed by ... (observed at: dchub://audit/SH52-076). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100143). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-15T07:40:38.282798Z · inv #100143_

## The approved recommendation

Choose one: (A) treat this as config/ops — pull the live _routes.json and zone-worker allowlist, exclude /markets static paths from the worker ASSETS rewrite, and arm a Pages-vs-worker routing assertion in CI; or (B) first re-verify whether commit 70ba69551abe already fixed the live symptom and only the missing guard remains. No mechanical single-file remedy is proposed because no file contents were available in evidence to source a verbatim, provably-unique find string, and the prior finding places the root cause in routing config, not a code file.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
