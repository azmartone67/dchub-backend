<!-- fingerprint:8770b4efb1a3c271c5c12c3eb29a67e7 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: cross_surface_metric_divergence @ routes/mcp_presence_crawler.py:2403 (seen

> Auto-captured from an **approved** brain agenda item (#100257). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-09T23:42:27.759799Z · agenda #100257_

## The approved recommendation

Open a CI check in the dchub-backend repo that greps route files under routes/ for bare integer literals matching known canonical values (markets, facilities, countries) and fails the build unless the value is sourced via canonical_stats.get_canonical_stats()/markets_phrase(); seed it by first re-reading routes/mcp_presence_crawler.py:2403 to confirm which canonical metric is hardcoded there.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
