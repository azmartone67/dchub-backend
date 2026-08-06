<!-- fingerprint:421fb0fc279c4de87a52dd8dc4f7e0ec -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — 1 public page(s) never finish redirecting — observed from the anon seat on contract: /press never lands — redirect loop; 8 page(s) landed, 0 unreachable What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100030). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-06T22:38:07.999962Z · inv #100030_

## The approved recommendation

Approve running the diagnostic trace (curl -sIL https://dchub.cloud/press) and, if it shows the fallback loop, trigger `gh workflow run press-rss.yml` in dchub-frontend to rebake press.html — versus escalating straight to an edit of the frontend rewrite rules. Also explicitly confirm the sentinel manifest should NOT be relaxed for /press.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
