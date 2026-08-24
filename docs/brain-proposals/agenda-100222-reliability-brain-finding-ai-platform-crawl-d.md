<!-- fingerprint:2694df170c85776bf3e88a79f197b442 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: ai_platform_crawl_drop:claude @ ai_requests (seen x3477)

> Auto-captured from an **approved** brain agenda item (#100222). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-24T17:13:55.261399Z · agenda #100222_

## The approved recommendation

Choose the scope: (A) detector-lifecycle fix only (stop the 3477-fold noise, accept current Claude baseline), (B) lifecycle fix + outside-in Claude crawlability probe (robots/llms.txt/WAF verification) to restore or definitively explain the lost crawl volume, or (C) accept the new low-Claude regime and re-anchor the baseline with no probe. Also decide whether to audit why the 2026-07-12 fix (brain_findings/9679) failed to stop recurrence before shipping anything new.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
