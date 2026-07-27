<!-- fingerprint:70ec1978d9200acddddf95286ebb23db -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: mcp_presence_listing_stale @ mcp_presence_listings (seen x10)

> Auto-captured from an **approved** brain agenda item (#100145). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-27T03:27:12.539412Z · agenda #100145_

## The approved recommendation

Approve building the structural fix — per-registry-row SLA evaluation in the health check plus an automatic re-scrape trigger on SLA breach (with owner-gated registries routed to a separate human-loop queue) — versus continuing to manually patch each mcp_presence_listing_stale instance as it recurs.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
