<!-- fingerprint:cb1561c0845666c89bd6af0b028b17d4 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — glama advertises 21,000 facilities (canon 17,388) — observed from the none seat on registry: listing prose says 21,000+ facilities; the platform's own live canon says 17,388 (over) What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100076). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-10T14:37:16.003038Z · inv #100076_

## The approved recommendation

Approve the two-part fix: (a) update the Glama listing prose to '17,000+ verified facilities of 25,000+ tracked across 178 countries' (or your preferred round-down phrasing — first resolve whether 17,388 or 17,469 is canonical), and (b) add '21,000' to PINNED['stale_markers']. Also decide whether the open mcp_registry_listing_stale detector item should be closed against this fix or investigated as a separate listing.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
