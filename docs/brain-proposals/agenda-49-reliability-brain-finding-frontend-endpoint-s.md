<!-- fingerprint:1ccb40abb94975272eb2b57ec800e826 -->
# Brain proposal — [reliability] Brain finding: frontend_endpoint_slow @ /dc-hub-media (value 5,244)

> Auto-captured from an **approved** brain agenda item (#49). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-02T23:29:18.338131Z · agenda #49_

## The approved recommendation

Choose the remediation path: (a) approve pulling the metric definition + APM/timing breakdown for /dc-hub-media before any change, (b) proceed directly with CDN/edge-cache offload + cache headers as the structural fix, or (c) first unblock the stuck Cloudflare Pages deploy (finding count 10) in case the slow path is a stale deployment — and set the explicit healthy-range threshold (in the metric's confirmed unit) the brain should validate against post-fix.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
