<!-- fingerprint:6ff4a140f7011a190e61694f9590d873 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: ai_platform_crawl_drop:you @ ai_requests (seen x503)

> Auto-captured from an **approved** brain agenda item (#100280). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-23T06:12:56.994758Z · agenda #100280_

## The approved recommendation

Open the ai_platform_crawl_drop detector definition (the query in main.py near line 27028 reading ai_requests GROUP BY platform) and add a rolling-baseline threshold plus a platform+day dedup key so it emits one finding per sustained drop instead of per-scan, then confirm on the live worklist that the 503 count collapses toward single-digit distinct findings.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
