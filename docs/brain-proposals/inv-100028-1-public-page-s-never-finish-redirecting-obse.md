<!-- fingerprint:421fb0fc279c4de87a52dd8dc4f7e0ec -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — 1 public page(s) never finish redirecting — observed from the anon seat on contract: /press never lands — redirect loop; 8 page(s) landed, 0 unreachable What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100028). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-06T03:04:24.444119Z · inv #100028_

## The approved recommendation

Approve running the diagnostic curl trace against /press from the anon seat, then choose between (a) re-running the press bake workflow (gh workflow run press-rss.yml in dchub-frontend) if the artifact is missing, or (b) authorizing a one-line edit to the specific frontend rewrite/redirect rule the trace identifies — and decide whether the site_sentinel manifest expectation for /press should instead be adjusted.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
