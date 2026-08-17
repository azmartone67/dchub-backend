<!-- fingerprint:1ccb40abb94975272eb2b57ec800e826 -->
# Brain proposal — [reliability] Brain finding: frontend_endpoint_slow @ /dc-hub-media (value 5,244)

> Auto-captured from an **approved** brain agenda item (#49). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-02T23:29:18.338131Z · agenda #49_

## The approved recommendation

Choose the remediation path: (a) approve pulling the metric definition + APM/timing breakdown for /dc-hub-media before any change, (b) proceed directly with CDN/edge-cache offload + cache headers as the structural fix, or (c) first unblock the stuck Cloudflare Pages deploy (finding count 10) in case the slow path is a stale deployment — and set the explicit healthy-range threshold (in the metric's confirmed unit) the brain should validate against post-fix.

## Rolled-up targets — class `frontend_endpoint_slow` (class collapse, 2026-08-17)

This doc is now the single obligation for **5 occurrences** of
`frontend_endpoint_slow`. The other 4 were closed against it. They are listed here
in full so the collapse loses no target — fixing the class means fixing
every line below, and a fix that only covers this doc's own target has not
discharged the obligation.

- `/dc-hub-media` — was `agenda-49-reliability-brain-finding-frontend-endpoint-s.md` (filed 2026-07-02)
- `/construction-pipeline` — was `agenda-72-reliability-brain-finding-frontend-endpoint-s.md` (filed 2026-07-09)
- `/ai-pipeline` — was `agenda-100095-reliability-brain-finding-frontend-endpoint-s.md` (filed 2026-07-14)
- `/` — was `agenda-100097-reliability-brain-finding-frontend-endpoint-s.md` (filed 2026-07-14)
- `/dashboard` — was `agenda-100100-reliability-brain-finding-frontend-endpoint-s.md` (filed 2026-07-15)

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
