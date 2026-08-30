<!-- fingerprint:e191851d5e8a4f3f735d3a4b8992b2da -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: cf_cache_rate_low @ cache_rate:24.38% (seen x1215)

> Auto-captured from an **approved** brain agenda item (#100239). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-30T21:02:35.127199Z · agenda #100239_

## The approved recommendation

Approve the two-track plan: (a) ship the detector change (stable finding key + occurrence counter + refire cooldown in brain_consistency_radar.py, plus a staleness guard on the cf_analytics cache_rate read), and (b) authorize pulling route-level Cloudflare cache hit/miss data before any CDN configuration change — or decide instead to accept the low cache rate and only silence the detector, which is cheaper but leaves the possible real 24.38% cache rate unaddressed.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
