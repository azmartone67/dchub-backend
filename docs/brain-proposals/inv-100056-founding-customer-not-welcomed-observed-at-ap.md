<!-- fingerprint:5de1b585699366fcf5b96aaa13183dd3 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — founding_customer_not_welcomed (observed at: /api/v1/admin/founding-customers/send-welcome). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100056). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-09T19:46:46.786741Z · inv #100056_

## The approved recommendation

Decide whether to (a) manually check Railway's DCHUB_RESEND_API_KEY and the Resend dashboard delivery log for tj@karklins.com and resend if absent, or (b) instrument the send-welcome handler to return non-200 / log a delivery ID when the email provider call fails, so a 200 can no longer mask silent non-delivery. No mechanical code fix is proposed because the endpoint returns 200 on all observed calls and no source code or unique find string exists in the evidence.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
