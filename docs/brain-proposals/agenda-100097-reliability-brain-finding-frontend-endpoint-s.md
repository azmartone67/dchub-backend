# Brain proposal — [reliability] Brain finding: frontend_endpoint_slow @ / (value 5,840)

> Auto-captured from an **approved** brain agenda item (#100097). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-14T09:09:04.885763Z · agenda #100097_

## The approved recommendation

Decide whether to (a) prioritize unblocking the Cloudflare Pages deploy pipeline (the 10× cf_pages_deploy_stuck signal) as the presumed root cause for the ~5.8s-class latency on '/' and '/dashboard', or (b) first instrument the frontend_endpoint_slow detector to expose the metric's unit and healthy threshold (per HEALTH_BASELINE.md) before committing engineering effort — and confirm whether 5840 is milliseconds or something else.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
