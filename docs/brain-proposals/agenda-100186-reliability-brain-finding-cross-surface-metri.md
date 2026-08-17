<!-- fingerprint:95292e7e3b6b6062912e23569611383f -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: cross_surface_metric_divergence @ routes/competitive_seo.py:210 (seen x20)

> Auto-captured from an **approved** brain agenda item (#100186). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-10T08:51:05.377988Z · agenda #100186_

## The approved recommendation

Choose between: (A) approve the class-level fix — canonical_stats reads everywhere + CI lint fence banning stat literals in routes/ + cross-surface regression test — or (B) first run a verification pass to confirm whether the 2026-06-25 fix already covers competitive_seo.py:210 and the 20 open instances are detector staleness, then scope (A) accordingly. Also decide whether the lint fence should hard-fail builds or start as a warning.

## Triage — 2026-08-17 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/inv-100042-cross-surface-metric-divergence-observed-at-ro.md`, which stays
OPEN as the single obligation for `cross_surface_metric_divergence`. This doc's target —
`routes/competitive_seo.py:210` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on inv-100042-cross-surface-metric-divergence-observed-at-ro.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is inv-100042-cross-surface-metric-divergence-observed-at-ro.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in inv-100042-cross-surface-metric-divergence-observed-at-ro.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against inv-100042-cross-surface-metric-divergence-observed-at-ro.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-17 as a class member of inv-100042-cross-surface-metric-divergence-observed-at-ro.md (class collapse)