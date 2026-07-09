# Brain proposal — [reliability] Brain finding: ai_platform_crawl_drop:chatgpt @ ai_requests (seen x940)

> Auto-captured from an **approved** brain agenda item (#74). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-09T06:28:15.263704Z · agenda #74_

## The approved recommendation

Approve (a) building a stateful finding lifecycle with fingerprint dedup and landing-verified closure for brain detectors, and (b) a crawl-surface health fence (robots/llms.txt/sitemap render check + CF/WAF change audit) — versus continuing to triage each ai_platform_crawl_drop emission individually. Also decide whether to prioritize an immediate Cloudflare/WAF change review as the suspected shared root cause for the ChatGPT (and Meta) crawl drops.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
