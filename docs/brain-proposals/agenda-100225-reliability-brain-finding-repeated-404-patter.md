<!-- fingerprint:a9a046a82b66146d4b9c0f215628739d -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: repeated_404_pattern @ /api/v1/admin/crawler-status (seen x71)

> Auto-captured from an **approved** brain agenda item (#100225). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-25T08:24:22.839687Z · agenda #100225_

## The approved recommendation

Approve (a) adding caller-attribution fields (User-Agent/Referer/IP class) to the 404-capture middleware, and (b) after attribution, choose between implementing/aliasing /api/v1/admin/crawler-status vs fixing/removing the caller — plus whether to fund the route-contract CI check that would prevent this entire finding class.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
