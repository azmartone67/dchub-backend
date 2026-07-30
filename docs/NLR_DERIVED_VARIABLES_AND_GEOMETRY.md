# DC Hub × NLR — Derived Variables & Geometry Reference

**Version 1 — 2026-07-15 — for JSC / reVeal technical review**

This document is the data-dictionary companion to the NLR Intelligence Layer and the
Phase 3 parcel-geometry enhancements shipped for reVeal. It defines every **derived
variable** DC Hub computes — the exact inputs, the formula, the output range, and how
to read it — plus the **geometry** methods that back the parcel and grid-headroom
surfaces.

Guiding principle, consistent with the MOU's trust-first posture: **every number here is
either MEASURED (computed from geometry you or DC Hub supply) or MODELED (a proxy score
from lookup tables + published resource data).** Each variable below is tagged so an NLR
researcher knows how much positional/estimation error to carry. Nothing here is a bare
number without a stated basis.

Satisfies MOU **Schedule E.1** (Data Dictionary deliverable) for the derived-intelligence
and geometry surfaces. Complements — does not replace — the machine-readable
`/api/v1/methodology` and `/api/v1/data-dictionary.json` endpoints (Schedule A.8).

Cross-references:
- [`NLR_REVEAL_INTEGRATION_GUIDE.md`](NLR_REVEAL_INTEGRATION_GUIDE.md) — endpoint wiring
- [`NLR_SCHEDULE_A_EXPANSION.md`](NLR_SCHEDULE_A_EXPANSION.md) — endpoint surface & schedules
- [`NLR_PRODUCT_ROADMAP.md`](NLR_PRODUCT_ROADMAP.md) — where these fit the roadmap

---

## TL;DR — what's new since the June 10 JSC kickoff

| Enhancement | Surface | Basis | What it gives reVeal |
|---|---|---|---|
| **Geodesic parcel geometry** | `POST /api/v1/analyze-parcel` | **Measured** | Acreage, centroid/representative_point, contiguity — from any GeoJSON, zero DC Hub positional error |
| **Hosted parcel-boundary lookup** | `GET /api/v1/parcels/lookup`, and `analyze-parcel` with bare `lat/lng` | **Measured** | Point → containing parcel polygon from hosted county/state GIS (pilot: Loudoun County VA) |
| **`data_basis` provenance block** | `analyze-parcel` response | — | First-class "measured vs modeled" flag on every geometry read |
| **Derived-variable dictionary** | this document | Mixed | Formal definition of all NLR Intelligence Layer scores |

---

## Part 1 — Geometry (MEASURED)

The geometry engine is **self-contained** — no PostGIS or Shapely dependency in the
request path. It operates on any caller-supplied WGS84 GeoJSON, so the interface works
whether the polygon comes from the caller or from DC Hub's hosted parcel layer.

Source: [`routes/interconnection_queues.py`](../routes/interconnection_queues.py)
(`_ring_area_m2`, `_ring_centroid`, `_members_from_geometry`, `api_analyze_parcel`) and
[`routes/parcels.py`](../routes/parcels.py) (hosted lookup).

### 1.1 `total_acres` / per-member `acres` — geodesic area

- **Input:** the outer ring(s) of a GeoJSON `Polygon` / `MultiPolygon`, as `[lng, lat]`
  vertex pairs (WGS84 / EPSG:4326).
- **Method:** spherical-excess line integral on a sphere of radius **R = 6,378,137 m**
  (WGS84 semi-major axis):

  ```
  A_m² = | Σᵢ  radians(lonᵢ₊₁ − lonᵢ) · (2 + sin(latᵢ) + sin(latᵢ₊₁)) · R² / 2 |
  acres = A_m² / 4046.8564224
  ```

- **Output:** `acres` per member (rounded 2 dp); `total_acres` = sum across members.
- **Basis:** **MEASURED.** Ground truth from the exact vertices supplied — DC Hub adds
  **0 m** of positional error. Accurate to well under a metre at parcel scale.
- **Why spherical-excess, not planar:** a planar (shoelace-in-degrees) area is wrong by
  the cosine-of-latitude distortion — material at continental-US latitudes. Spherical
  excess is latitude-correct without a projection step.

### 1.2 `representative_point` — the anchor centroid

- **Method:** planar **shoelace centroid** of the **largest-area member**, returned as
  `{lat, lng}`. Degenerate rings fall back to the vertex mean.
- **Basis:** **MEASURED** (a few metres at parcel scale).
- **Design contract — read this:** the representative point is deliberately the centroid
  of the **single largest part**, *never* the multi-part geometric center. A multi-part
  centroid can land **off-parcel** — in a highway median or a river between two
  discontiguous lots — and would then poison every downstream point-keyed read
  (grid, fiber, water, verdict). The largest-member centroid is guaranteed on-parcel.

### 1.3 `contiguous` + `member_count` — discontiguity flag

- **`member_count`** = number of polygon members (1 for a `Polygon`; N for a
  `MultiPolygon`).
- **`contiguous`** = `member_count == 1`.
- **How to read `contiguous = false`:** the parcel is **discontinuous**. Treat setbacks,
  fencing, and point-of-interconnection **per member**, not as one summed footprint — the
  `total_acres` sum is real land but it is not one buildable pad.

### 1.4 Hosted parcel lookup — point → polygon

- **Surface:** `GET /api/v1/parcels/lookup?lat=&lng=[&include_geometry=1]`, and
  `analyze-parcel` when called with `lat`/`lng` and no `geometry`.
- **Method:** PostGIS `ST_Contains` against the `parcel_boundaries` table (hosted free
  county/state open-data layers). Returns `parcel_id`, `county`, `state`, source
  acreage (`acres_gis` / `acres_legal`), county attributes, and optionally the polygon in
  the exact GeoJSON shape `analyze-parcel` accepts — so the
  `analyze-parcel → analyze_site` rail lights up on hosted data.
- **Coverage:** rolling out by data-center-market priority. Pilot market: **Loudoun
  County VA (~132K polygons)**. Live markets: `GET /api/v1/parcels/coverage`.
- **Basis:** **MEASURED** (source polygon), with `acres_per_source` carried as published
  by the county (the source's number, not a DC Hub recomputation).
- **Scope honesty (proven 2026-07-06, do not re-litigate):** ISO interconnection-queue
  rows carry **no** parcel identity and are mostly county-centroid geocoded, so queue
  handoffs **never** auto-carry a `geometry`. This layer serves callers that already
  *have* a location (a real site, an address geocode, a map click) — not queue rows.

### 1.5 `data_basis` — provenance on every geometry read

`analyze-parcel` returns a first-class provenance block so an agent knows how much to
trust each number:

| Field | Meaning |
|---|---|
| `geometry_source` | `caller-supplied WGS84 GeoJSON` or `DC Hub hosted county/state GIS parcel layer (point lookup)` |
| `area_method` | `geodesic spherical-excess — measured, not modeled` |
| `centroid_method` | `planar shoelace of the largest-area member` |
| `positional_error_added_by_dchub_m` | `0` |

Contrast with the interconnection-queue handoff, where the anchor is a
`county_centroid` (approximate) vs `poi_exact` — carried there as
`coordinate_precision`. Geometry reads are the measured end of that spectrum.

---

## Part 2 — Derived variables: NLR Intelligence Layer (MODELED)

Source: [`nlr_intelligence.py`](../nlr_intelligence.py). Four routes, each returning a
headline score plus its component breakdown. These are **MODELED proxy scores** — they
combine published resource data (NREL solar GHI, NREL wind class, IRA/state incentive
proxies, USGS/NREL geothermal zones) with a live substation query. They are screening
indicators, not engineering-grade siting numbers; every route carries a caveat to that
effect.

**Scoring convention:** all scores are **0–100, higher = better** unless noted.

### 2.1 Shared inputs / lookup tables

| Input | Source | Notes |
|---|---|---|
| `SOLAR_GHI[state]` | NREL — kWh/m²/day | State-level global horizontal irradiance |
| `WIND_CLASS[state]` | NREL wind class 1–7 | Higher = stronger resource |
| `TAX_INCENTIVE_SCORES[state]` | IRA / state incentive proxy (0–100) | Modeled proxy, not a dollar figure |
| `GEOTHERMAL_ZONES` | USGS EGS Atlas + NLR/NREL research zones | 10 zones; each has a base potential 62–95 |
| `substations` table | HIFLD substation DB (hosted, Neon PG) | Live query by bounding box + haversine |

### 2.2 Component scores

| Variable | Formula | Range | Reads as |
|---|---|---|---|
| `solar_score` | step on GHI: ≥6.0→95, ≥5.5→85, ≥5.0→75, ≥4.5→65, ≥4.0→52, else 40 | 40–95 | Solar resource quality |
| `wind_score` | `min(95, max(10, wind_class × 13))` | 10–95 | Wind resource quality |
| `geothermal effective_score` | `base_potential × decay`, where `decay = max(0, 1 − d/radius × 0.5)` and `d` = haversine km to nearest zone | 0–95 | Proximity-weighted geothermal potential |
| `grid_access` | tiered on substation count + max voltage (see 2.3) | 15–100 | Transmission access density |
| `renewable_potential` | `round(solar×0.40 + wind×0.35 + min(geo,30)×0.25)` | 0–95 | Blended renewable resource (geo capped at 30 in the blend) |
| `storage_suitability` | `min(95, 45 + round(GHI × 8))` | ~45–95 | Climate proxy for battery cycle suitability |

**Geothermal distance decay:** a zone's score falls off **linearly to 50% of base at the
search radius edge** (default `radius_km = 500`). A site *on* a zone keeps full base
potential; a site at the radius edge keeps half. Zones beyond the radius are dropped.
`nearest_zone` / `nearest_zone_km` report the closest one.

### 2.3 `grid_access` tiers (from live substation query)

Substations are pulled from the `substations` table within a lat/lng bounding box, then
filtered by true haversine distance. `sub_count` = number within radius; `max_kv` =
highest voltage among them.

| Condition | `grid_access` |
|---|---|
| `sub_count == 0` | 15 |
| `sub_count < 3` | `35 + min(max_kv/10, 25)` |
| `sub_count < 10` | `55 + min(max_kv/10, 20)` |
| `sub_count ≥ 10` | `75 + min(max_kv/20, 20)` |

Capped at 100.

### 2.4 Headline composite scores

| Score | Route | Formula | Weighting rationale |
|---|---|---|---|
| `geothermal_score` | `/api/v1/geothermal-potential` | `effective_score` of the nearest zone | Proximity-weighted resource |
| `colocation_score` | `/api/v1/colocation-score` | `round(renewable_potential×0.40 + grid_access×0.25 + tax_incentives×0.20 + geothermal_bonus×0.15)` | Renewables-led co-location suitability |
| `microgrid_score` | `/api/v1/microgrid-viability` | `round(solar×0.30 + wind×0.25 + geothermal×0.20 + storage_suitability×0.25)` | Balanced generation + storage mix |
| `total_estimated_available_mw` | `/api/v1/grid-headroom` | Σ per-substation headroom (see 2.6) | Point-estimate transmission headroom |

### 2.5 Derived economics (colocation route)

| Variable | Formula | Units | Basis |
|---|---|---|---|
| `estimated_ppa_discount_pct` | `round((renewable_potential − 50) × 0.8, 1)` if `renewable_potential > 50` else `0` | % | MODELED indicator, not a quote |
| `carbon_reduction_potential_pct` | `round((solar_score + wind_score) / 2 × 0.9, 1)` | % | MODELED indicator |

> **Read as directional.** These are screening indicators derived from resource scores,
> not PPA quotes or verified emissions figures. Actual PPA pricing and carbon accounting
> require utility/offtaker confirmation.

### 2.6 Grid-headroom estimate (point estimator)

`total_estimated_available_mw` sums a per-substation headroom drawn from voltage class,
for up to the 10 nearest substations:

| Voltage class | Est. available MW/substation |
|---|---|
| ≥ 500 kV | 800 |
| ≥ 345 kV | 500 |
| ≥ 230 kV | 300 |
| ≥ 138 kV | 150 |
| ≥ 115 kV | 100 |
| ≥ 69 kV | 50 |
| < 69 kV | 20 |

Rating bands: Abundant (≥2 GW) · Strong (1–2 GW) · Adequate (500 MW–1 GW) · Moderate
(200–500 MW) · Constrained (<200 MW).

> **★ Honesty fix (2026-06-26), important for reVeal:** `/api/v1/grid-headroom` is a
> **point** estimator — it needs a `lat`/`lon`. It does **not** resolve an ISO/region to
> real geography. When called for an ISO/region with no explicit point, it returns
> `available: false, reason: "not_region_specific"` and points to
> `/api/v1/grid/intelligence/<ISO>` — it will **not** substitute a default point and
> mislabel one location's headroom as an ISO's. Use the point estimator for a specific
> site; use grid-intelligence for ISO-level demand/fuel-mix/queue data.

### 2.7 ARIES / research-alignment flags (microgrid route)

Boolean flags, not scores — surfaced for NLR ARIES program alignment:

| Flag | Condition |
|---|---|
| `islanding_candidate` | `microgrid_score ≥ 65` |
| `high_renewable_fraction` | `(solar + wind)/2 ≥ 65` |
| `geothermal_baseload` | `geothermal ≥ 50` |
| `storage_integration` | `storage_suitability ≥ 60` |
| `dc_powerplant_concept` | `geothermal ≥ 80 AND microgrid_score ≥ 75` |

The `recommended_configuration` array (solar / wind / optional geothermal baseload /
battery MWh) is sized off the caller's `capacity_mw` using fixed DC-load ratios
(solar 1.5×, wind 0.8×, geothermal 0.4× when the zone qualifies, storage 2× MWh).

---

## Part 3 — Access tiering (how these variables gate)

The derived-intelligence routes are tier-gated (r43-H, 2026-05-28). NLR's issued key,
the reVeal integration, DC Hub internal callers, and real dchub.cloud browsers receive
the **full** breakdown. Anonymous external `curl` traffic receives a **headline teaser**
(location + top-line score + upgrade hint) with the granular component breakdown and
geocoded substation detail withheld. The gate **fails open** on infrastructure error so
a gate hiccup never blanks a legitimate NLR call.

---

## Provenance & citation

- Geometry: **measured** — geodesic spherical-excess area, planar-shoelace centroid, on
  caller- or DC-Hub-hosted WGS84 GeoJSON. Zero DC Hub positional error.
- Scores: **modeled** — NREL (solar GHI, wind class), USGS EGS Atlas + NLR/NREL research
  zones (geothermal), IRA/state incentive proxies, HIFLD substation DB (grid).
- Cite as: **"DC Hub (dchub.cloud)"**. Parcel reads carry `CC-BY-4.0`.

*Prepared for the DC Hub × NLR partnership. Formulas mirror production code as of
2026-07-15; see linked source files for the authoritative implementation.*
