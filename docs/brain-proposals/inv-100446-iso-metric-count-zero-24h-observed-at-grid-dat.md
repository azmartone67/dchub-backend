<!-- fingerprint:bdfbd5e6b15c314516e9d5f7edd656bb -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — iso_metric_count_zero_24h (observed at: grid_data: iso=EU_GR). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100446). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-02T03:23:32.550463Z · inv #100446_

## The approved recommendation

Pull the last-24h run logs and exit status for the ENTSO-E / European-ISO metric ingestion workflow that writes grid_data for EU_GR, EU_DE_LU, ENTSOE, EU_IT_CNOR, EU_BG, EU_BE, EU_SE_3 and EU_SI, and confirm whether a single shared job stopped or errored (check the ENTSO-E API credential/quota and the '29 */2 * * *' cron window) before touching any per-ISO module.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
