<!-- fingerprint:5a2bcb091ed4d247af24bcdce245d0b4 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — csp_violation_recurring (observed at: csp://script-src-elem/appassets.androidplatform.net). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100362). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-24T06:15:34.104014Z · inv #100362_

## The approved recommendation

Choose one of three paths for the appassets.androidplatform.net CSP violation: (a) accept it as benign Android-WebView wrapper noise and leave the CSP unchanged (recommended), (b) add a suppression/ignore rule for this hostname in the /api/csp-report handler to stop the recurring finding, or (c) if DC Hub sanctions an Android app embedding the site, allowlist the domain in dchub-frontend/_headers script-src-elem. No mechanical remedy block is emitted because the domain does not appear anywhere in the evidenced code, the _headers file contents were never shown, and the fix is a security judgement call rather than a verifiable single-file find-and-replace.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
