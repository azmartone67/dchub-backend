<!-- fingerprint:8866ac01bf4f577521c6fd6a5b70d617 -->
# Brain proposal — [reliability] Brain finding: ai_platform_crawl_drop:chatgpt @ ai_requests (seen x940)

> Auto-captured from an **approved** brain agenda item (#74). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-09T06:28:15.263704Z · agenda #74_

## The approved recommendation

Approve (a) building a stateful finding lifecycle with fingerprint dedup and landing-verified closure for brain detectors, and (b) a crawl-surface health fence (robots/llms.txt/sitemap render check + CF/WAF change audit) — versus continuing to triage each ai_platform_crawl_drop emission individually. Also decide whether to prioritize an immediate Cloudflare/WAF change review as the suspected shared root cause for the ChatGPT (and Meta) crawl drops.

## Rolled-up targets — class `ai_platform_crawl_drop` (class collapse, 2026-08-17)

This doc is now the single obligation for **3 occurrences** of
`ai_platform_crawl_drop`. The other 2 were closed against it. They are listed here
in full so the collapse loses no target — fixing the class means fixing
every line below, and a fix that only covers this doc's own target has not
discharged the obligation.

- `ai_requests` — was `agenda-74-reliability-brain-finding-ai-platform-crawl-d.md` (filed 2026-07-09)
- `ai_requests` — was `inv-100065-ai-platform-crawl-drop-copilot-observed-at-ai.md` (filed 2026-08-10)
- `ai_requests` — was `agenda-100193-reliability-brain-finding-ai-platform-crawl-d.md` (filed 2026-08-13)

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
