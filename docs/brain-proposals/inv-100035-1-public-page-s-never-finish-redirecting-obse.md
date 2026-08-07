<!-- fingerprint:421fb0fc279c4de87a52dd8dc4f7e0ec -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — 1 public page(s) never finish redirecting — observed from the anon seat on contract: /press never lands — redirect loop; 8 page(s) landed, 0 unreachable What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100035). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-07T03:21:29.943043Z · inv #100035_

## The approved recommendation

Choose one: (a) treat /press as fixed by PR #2305 and close after a single anon-seat re-probe confirms it lands (recommended, cheapest), or (b) authorize a full redirect-chain trace (curl -v from anon seat capturing every 3xx + Location header) and a new fix at the layer #2305 identified, if the re-probe still shows a loop.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
