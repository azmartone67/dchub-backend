<!-- fingerprint:9bbf9f7e3f3efef16ad59e0f7b6452c9 -->
# Brain proposal — [reliability] Brain finding: runtime_error:ArcGIS error from https://services<n>.arcgis.com/Hp<n>G<n>Pky

> Auto-captured from an **approved** brain prop item (#100053). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-17T20:15:24.098137Z · prop #100053_

## The approved recommendation

Approve (1) building the external-GIS endpoint registry with health-probe + circuit-breaker + finding dedup as the permanent fix, and (2) whether to source refreshed URLs for the four dead layers versus dropping the ArcGIS dependency in favor of a cached/alternative dataset for substations, power plants, and natural-gas infrastructure.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
