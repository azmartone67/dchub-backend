<!-- fingerprint:8770b4efb1a3c271c5c12c3eb29a67e7 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: cross_surface_metric_divergence @ routes/mcp_presence_crawler.py:2403 (seen

> Auto-captured from an **approved** brain agenda item (#100257). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-09T23:42:27.759799Z · agenda #100257_

## The approved recommendation

Open a CI check in the dchub-backend repo that greps route files under routes/ for bare integer literals matching known canonical values (markets, facilities, countries) and fails the build unless the value is sourced via canonical_stats.get_canonical_stats()/markets_phrase(); seed it by first re-reading routes/mcp_presence_crawler.py:2403 to confirm which canonical metric is hardcoded there.

## Triage — 2026-09-26 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/inv-100042-cross-surface-metric-divergence-observed-at-ro.md`, which stays
OPEN as the single obligation for `cross_surface_metric_divergence`. This doc's target —
`routes/mcp_presence_crawler.py:2403 (seen` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on inv-100042-cross-surface-metric-divergence-observed-at-ro.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is inv-100042-cross-surface-metric-divergence-observed-at-ro.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in inv-100042-cross-surface-metric-divergence-observed-at-ro.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against inv-100042-cross-surface-metric-divergence-observed-at-ro.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-09-26 as a class member of inv-100042-cross-surface-metric-divergence-observed-at-ro.md (class collapse)