<!-- fingerprint:b46f139ebef752d82a9addb60b7ea5d8 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: site_sentinel_unhealthy:/operators/digital-realty/brief @ https://dchub.clo

> Auto-captured from an **approved** brain agenda item (#100175). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-06T18:20:18.292575Z · agenda #100175_

## The approved recommendation

Decide whether /operators/<slug>/brief pages are still an intended product surface: if YES, approve a single route-level fix to restore the brief route for all operators; if NO, approve removing those entries from the site_sentinel manifest. In either case, approve adding a CI guard that fails deploys when sentinel manifest URLs don't resolve to registered routes, and a dedup rule so one root cause stops generating hundreds of repeat findings.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
