<!-- fingerprint:cb1561c0845666c89bd6af0b028b17d4 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — glama advertises 21,000 facilities (canon 17,866) — observed from the none seat on registry: listing prose says 21,000+ facilities; the platform's own live canon says 17,866 (over) What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100146). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-15T07:42:30.291724Z · inv #100146_

## The approved recommendation

Choose the advertised canonical figure and mechanism: (a) confirm whether PR #187's daily registry sync rewrites the Glama facility-count prose and, if not, add that field to the sync; (b) decide whether to advertise exact live verified (17,866) or a rounded floor ('17,800+' / 'over 17,500 verified'); (c) approve adding '21,000' to the stale_markers scrub list; and (d) resolve which verified count (17,866 vs 18,121, per issue #1539's filter change) is the single source of truth before any copy is regenerated.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
