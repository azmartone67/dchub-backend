<!-- fingerprint:4e57f368e44f0cc6df4e505937b9737c -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — api_contract_unreachable: daily_returns_html (observed at: https://dchub.cloud/daily). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100220). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-18T19:13:11.810152Z · inv #100220_

## The approved recommendation

Decide whether to (a) run the live verification (curl -i https://dchub.cloud/daily + inspect the daily_returns_html handler and its template) and triage against dchub-backend issue #1404, or (b) close this finding as stale if the endpoint is confirmed healthy post the #2866–#2870 merges. No mechanical remedy block is provided because no exact file path or verbatim find string exists in the evidence — a guessed patch is prohibited.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
