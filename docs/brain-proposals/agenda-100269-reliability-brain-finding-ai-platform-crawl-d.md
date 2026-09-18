<!-- fingerprint:a0a54aeb84b6e1720981a33e2642b832 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: ai_platform_crawl_drop:perplexity @ ai_requests (seen x2725)

> Auto-captured from an **approved** brain agenda item (#100269). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-18T00:59:31.213876Z · agenda #100269_

## The approved recommendation

Run the platform GROUP BY query behind main.py:25834 (SELECT platform, COUNT(*) FROM ai_requests) bucketed by day for the last 30 days for platform='perplexity', and fetch the Perplexity crawler's live robots.txt/llms.txt/sitemap plus any WAF/bot rule matching its user-agent, to confirm whether the drop coincides with a specific deploy or gating change before writing the fetchability guard.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
