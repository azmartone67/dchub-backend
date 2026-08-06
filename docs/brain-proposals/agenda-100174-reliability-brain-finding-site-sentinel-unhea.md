<!-- fingerprint:76d4e53fc4706c7be7d0d9ce00f2938a -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: site_sentinel_unhealthy:/operators/aligned/brief @ https://dchub.cloud/oper

> Auto-captured from an **approved** brain agenda item (#100174). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-06T02:56:30.374743Z · agenda #100174_

## The approved recommendation

Decide the product intent for /operators/<slug>/brief: (A) restore it as a live, data-backed route class and couple the Site Sentinel manifest to the route table with a deploy-blocking contract check (possibly extending PR #2268's QA surface), or (B) formally deprecate the brief pages and delete their entries from routes/site_sentinel.py:_MANIFEST. Also confirm whether #2268 already covers deploy-time page contracts so we extend rather than rebuild.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
