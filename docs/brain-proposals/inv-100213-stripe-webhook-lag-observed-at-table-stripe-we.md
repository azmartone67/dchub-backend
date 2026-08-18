<!-- fingerprint:725bb6f1f932f779aa502dbb3c141f96 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — stripe_webhook_lag (observed at: table:stripe_webhook_events). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100213). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-18T19:13:36.980965Z · inv #100213_

## The approved recommendation

Open the Stripe dashboard (Developers → Webhooks) and determine whether the /stripe/webhook endpoint is disabled or returning repeated 5xx errors. If disabled, re-enable it and replay missed events to reconcile subscription/payment state; if it is healthy on Stripe's side, commission a code audit of the webhook ingestion path and the check_stripe_webhook_lag detector (for sequential HTTP calls or missing per-probe timeouts) before any code change is proposed. No mechanical patch is offered because no unique find-string exists in the evidence — this is an ops/config decision.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
