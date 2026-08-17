<!-- fingerprint:d44add71b2ffb13876c9fb28fd4592dc -->
# Brain proposal — [reliability] Brain finding: frontend_endpoint_slow @ /dashboard (value 5,941)

> Auto-captured from an **approved** brain agenda item (#100100). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-15T07:48:00.285613Z · agenda #100100_

## The approved recommendation

Decide whether to authorize the two remediation tracks — (a) clear/redeploy the stuck Cloudflare Pages build and (b) refactor the 5 unsafe DB connection patterns in routes/deals_routes.py — and confirm the healthy latency threshold from HEALTH_BASELINE.md that the /dashboard metric (currently 5941) must stay under, across multiple detector passes, before this finding is marked resolved.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
