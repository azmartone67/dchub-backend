<!-- fingerprint:9bbf9f7e3f3efef16ad59e0f7b6452c9 -->
# Brain proposal — [reliability] Brain finding: runtime_error:ArcGIS error from https://services<n>.arcgis.com/Hp<n>G<n>Pky

> Auto-captured from an **approved** brain prop item (#100053). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-17T20:15:24.098137Z · prop #100053_

## The approved recommendation

Approve (1) building the external-GIS endpoint registry with health-probe + circuit-breaker + finding dedup as the permanent fix, and (2) whether to source refreshed URLs for the four dead layers versus dropping the ArcGIS dependency in favor of a cached/alternative dataset for substations, power plants, and natural-gas infrastructure.

## Triage — 2026-08-17 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/prop-100041-reliability-brain-finding-runtime-error-sync.md`, which stays
OPEN as the single obligation for `runtime_error`. This doc's target —
`[reliability] Brain finding: runtime_error:ArcGIS error from` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on prop-100041-reliability-brain-finding-runtime-error-sync.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is prop-100041-reliability-brain-finding-runtime-error-sync.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in prop-100041-reliability-brain-finding-runtime-error-sync.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against prop-100041-reliability-brain-finding-runtime-error-sync.md
- [x] Or close this PR if superseded / not worth it — closed 2026-08-17 as a class member of prop-100041-reliability-brain-finding-runtime-error-sync.md (class collapse)