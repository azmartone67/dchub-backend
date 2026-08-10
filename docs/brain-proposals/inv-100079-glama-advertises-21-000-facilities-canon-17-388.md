<!-- fingerprint:cb1561c0845666c89bd6af0b028b17d4 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — glama advertises 21,000 facilities (canon 17,388) — observed from the none seat on registry: listing prose says 21,000+ facilities; the platform's own live canon says 17,388 (over) What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100079). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-10T19:32:02.166944Z · inv #100079_

## The approved recommendation

Choose which figure glama should advertise: the verified fleet ('17,000+ verified facilities', round-down floor of 17,388) or the tracked discovery pile ('25,000+ tracked'). Then approve (a) resubmitting/editing the glama listing with that copy, and (b) adding '21,000' to PINNED['stale_markers'] so the scrubber suppresses reprints. Separately decide whether the 17,388-vs-17,469 verified-count drift warrants its own investigation.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
