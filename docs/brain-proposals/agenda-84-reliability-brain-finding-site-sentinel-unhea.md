# Brain proposal — [reliability] Brain finding: site_sentinel_unhealthy:/admin/funnel-health @ https://dchub.cloud/admin/fu

> Auto-captured from an **approved** brain agenda item (#84). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-11T08:04:07.749345Z · agenda #84_

## The approved recommendation

Choose the remediation path: (A) prioritize unblocking the 10 stuck Cloudflare Pages deploys as the presumed shared root cause of the /admin/* 502s, (B) split the sentinel check to validate the funnel-health data API independently of the page render, or (C) both — and separately approve fixing the sentinel worklist so HTTP status codes are never reported as recurrence counts.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
