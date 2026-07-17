# Brain proposal — [reliability] Brain finding: site_sentinel_unhealthy:/admin/funnel-health @ https://dchub.cloud/admin/fu

> Auto-captured from an **approved** brain agenda item (#100105). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-17T03:17:41.075244Z · agenda #100105_

## The approved recommendation

Approve (a) prioritizing a code-level fix of the /admin/funnel-health handler with cached-snapshot rendering + fallback, and (b) a sentinel lifecycle change: dedupe same-fingerprint unhealthy findings into one chronic incident and require a verified 200 re-probe (post code change) before closure. Explicitly reject the 'adjust the manifest' alternative unless you decide the admin funnel page should not be a sentinel-tracked surface.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
