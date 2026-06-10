# DC Hub × NLR — Phase 2 Proposal

**Version 1 — 2026-06-09 (post-kickoff JSC reading)**

Surfaces the work agreed during the kickoff call. Three new proposed schedules (A.11–A.13) + one operational action (Enterprise-tier upgrade). All target reVeal's research workflow specifically.

---

## §0 — IMMEDIATE OPERATIONAL ACTION

### Upgrade all 3 NLR contacts to Enterprise tier with NLR Research Seed pricing

**Why:** the partner-key gateway bypass we shipped tonight (r78-b/c/d) lets the keys pass tier gates at the API layer, but the dashboard UI reads `users.plan` directly — which is still showing them as free. Premium features (Site Selection Canvas, Grid+Gas Transition Sentinel, Deal Autopsy, the new Schedule A.6-A.10 surface) need them on `plan='enterprise'`.

**How:** atomic SQL update in Neon Console, `docs/NLR_UPGRADE_TO_ENTERPRISE.sql`. Three tables updated in one transaction:
- `users.plan` → `'enterprise'`
- `api_keys.plan` + `rate_limit_tier` → `'enterprise'`
- `partner_keys_issued.plan` → `'enterprise'`

**Preserves:** the existing key strings (already in NLR's hands via tonight's re-send emails), and the `partner_keys_issued.label` audit trail showing the 90%-off NLR Research Seed pricing mechanism.

**Verification:** the SQL file includes a SELECT that confirms all 3 contacts land at `enterprise` across all 3 tables. Expected output documented inline.

**Idempotent:** safe to re-run.

---

## §1 — Schedule A.11 — Geometry (NEW SCHEDULE)

**Why this matters to NLR:** reVeal operates at 5.76km × 5.76km grid resolution per the March 2026 paper (NLR/PR-6A20-99256, pages 23-24). Today DC Hub returns `lat`/`lon` scalars on facilities and `slug`/`name` on metros. reVeal needs **geospatial primitives** to consume DC Hub output directly into its raster pipeline — polygons for region boundaries, points for facility addresses.

### Proposed endpoints

| Endpoint | Returns | Backed by |
|---|---|---|
| `/api/v1/geometry/region/<slug>` | GeoJSON Polygon (single feature) for a metro / ISO / state | OSM Overpass relations + manual curation for metros |
| `/api/v1/geometry/region/all?format=geojson` | GeoJSON FeatureCollection of all 232 markets | Same |
| `/api/v1/geometry/facility/<id>` | GeoJSON Point feature with full facility properties | `discovered_facilities` lat/lng |
| `/api/v1/geometry/facility/by-bbox?bbox=…` | GeoJSON FeatureCollection of facilities in a bounding box | Same |
| `/api/v1/geometry/transmission/<id>` | GeoJSON LineString feature for transmission lines | HIFLD transmission |
| `/api/v1/geometry/substation/<id>` | GeoJSON Point feature for substations | HIFLD 79K+ substations |

### Response shape — region polygon example

```json
{
  "type": "Feature",
  "geometry": {
    "type": "Polygon",
    "coordinates": [[[-77.7, 39.0], [-77.4, 39.0], ...]]
  },
  "properties": {
    "slug": "northern-virginia",
    "name": "Northern Virginia (Loudoun-Fairfax)",
    "iso": "PJM",
    "metro_population_2024": 7124000,
    "centroid": {"lat": 38.96, "lon": -77.46},
    "area_km2": 13567,
    "facility_count": 312,
    "dcpi_composite": 87.3,
    "data_source": "OSM Overpass + DC Hub curation",
    "last_updated": "2026-06-09T..."
  }
}
```

### Enrichment to existing endpoints

Also add optional `?include_geometry=1` parameter to existing endpoints to attach geometry inline (saves a round-trip):

- `/api/v1/facility?id=…&include_geometry=1` → adds `geometry: { type: "Point", … }`
- `/api/v1/search-facilities?…&include_geometry=1` → each result includes Point
- `/api/v1/market-intel?slug=…&include_geometry=1` → adds Polygon
- `/api/v1/dcpi/scores?…&include_geometry=1` → each market with Polygon

### Delivery commitment

**60 days from JSC consensus.** Polygon + point endpoints first; line/multipolygon for transmission corridor second pass.

### Schedule A amendment text (paste-ready for MOU addendum)

> **Schedule A.11 — Geometry Endpoints**
>
> Six (6) endpoints providing GeoJSON-formatted geospatial primitives for licensed regions and facilities. Polygon features for metros, ISOs, and states. Point features for facilities and substations. LineString features for transmission corridors. Compatible with standard GIS tooling (QGIS, ArcGIS, geopandas) and with reVeal's 5.76km grid pipeline.

---

## §2 — Schedule A.12 — Source Attribution and Data Provenance (NEW SCHEDULE)

**Why this matters to NLR:** the validation paper needs to attribute every data point. Right now `/api/v1/methodology/data-dictionary.json` (shipped this week, satisfies Schedule E.1) gives high-level methodology, but doesn't document per-field provenance — i.e., "this `nearest_substation_km` value came from HIFLD-2024-Q2, dataset version 4.1, accessed 2026-06-09, derived via Haversine from the canonical substation point."

### Proposed endpoints

| Endpoint | Returns |
|---|---|
| `/api/v1/provenance/<category>` | Full source attribution for a data category (data-centers, transmission, gas, utility-load) |
| `/api/v1/provenance/sources` | Master list of all upstream sources with license terms |
| `/api/v1/provenance/derived` | Map of derived fields → primary sources they're computed from |

### Response shape — provenance for "data-centers" example

```json
{
  "category": "data_centers",
  "canonical_table": "discovered_facilities",
  "sources": [
    {
      "name": "Baxtel",
      "type": "primary",
      "url": "https://baxtel.com",
      "license": "Commercial — DC Hub licensed",
      "fields_provided": ["facility_name", "operator", "campus", "lat", "lng", "address"],
      "refresh_cadence": "weekly via API",
      "last_ingest": "2026-06-08T..."
    },
    {
      "name": "DC Hub Discovery (auto)",
      "type": "primary",
      "url": "internal",
      "license": "DC Hub-derived",
      "fields_provided": ["power_mw", "capacity_planned_mw", "construction_status", "iso_region"],
      "method": "Auto-discovery via news + filings + ISO interconnection-queue cross-reference",
      "refresh_cadence": "continuous (cron every 4h)",
      "last_ingest": "2026-06-09T..."
    },
    {
      "name": "Manual research",
      "type": "secondary",
      "fields_provided": ["operator", "primary_use", "tenant_known"],
      "method": "DC Hub team curation, source-attributed per row",
      "last_ingest": "varies per row"
    }
  ],
  "derived_fields": [
    {
      "field": "composite_score",
      "computed_from": ["power_mw", "iso_grid_intel", "fiber_proximity", "water_stress"],
      "method": "DC Hub composite scoring algorithm v2.1 (documented at /methodology)",
      "data_lineage_url": "/api/v1/methodology/composite-score"
    },
    {
      "field": "nearest_substation_km",
      "computed_from": ["lat", "lng", "HIFLD_substations.geometry"],
      "method": "Haversine distance from facility point to nearest HIFLD substation"
    }
  ],
  "confidence_scoring": {
    "method": "Per-field confidence based on source primacy + recency",
    "documented_at": "/api/v1/methodology/confidence"
  }
}
```

### Source coverage map (proposed initial documentation)

| Category | Upstream sources | Derivation status |
|---|---|---|
| **Data centers** | Baxtel + DC Hub auto-discovery + manual curation | Mixed: lat/lon raw; power_mw derived from filings/queue cross-ref; composite_score fully derived |
| **Transmission** | HIFLD-2024-Q2 + OSM Overpass | Raw geometry; "transmission_corridor" classification derived |
| **Gas** | EIA + OSM (gas compressor stations) + DC Hub commercial intel | Mixed: pipeline geometry raw; pricing curves derived from EIA + spot market |
| **Utility load** | ISO real-time feeds (PJM, ERCOT, CAISO, MISO, ISO-NE, NYISO, SPP) + EIA | Raw timeseries; reserve_margin + queue_depth derived from raw |

### Delivery commitment

**30 days from JSC consensus** (the data dictionary infrastructure already exists — this extends it with per-category provenance JSON Schema).

### Schedule A amendment text

> **Schedule A.12 — Source Attribution and Data Provenance**
>
> Three (3) endpoints providing per-category source attribution: which upstream sources contribute which fields, refresh cadence, last-ingest timestamp, license terms, and a derived-fields map identifying computed values + their primary inputs. Sources include Baxtel, HIFLD, EIA, OSM Overpass, ISO real-time feeds, and DC Hub auto-discovery. Compatible with peer-reviewed publication reproducibility requirements.

---

## §3 — Schedule A.13 — Historical Journey (Entity Lineage) (NEW SCHEDULE)

**Why this matters to NLR:** the validation paper compares reVeal projections at time T against operational reality at time T+N. Today DC Hub returns the *current* state of an entity. NLR needs the **time-series of changes** — when a facility was first discovered, when power_mw was revised, when construction_status flipped, etc.

### Proposed endpoints

| Endpoint | Returns |
|---|---|
| `/api/v1/facility/<id>/history` | Per-facility version history: every change to any field, with timestamp + source |
| `/api/v1/transmission/<id>/history` | Per-line transmission history (capacity changes, queue additions) |
| `/api/v1/gas/<facility_id>/history` | Per-gas-facility history |
| `/api/v1/utility-load/<iso>/history?from=…&to=…` | ISO load timeseries with revision history (forecast vs realized) |
| `/api/v1/lineage/<entity_type>/<entity_id>` | Unified lineage endpoint across all entity types |

### Response shape — facility history example

```json
{
  "facility_id": "dc-12345",
  "facility_name": "Equinix DC11",
  "history": [
    {
      "timestamp": "2024-03-15T...",
      "change_type": "discovery",
      "source": "Baxtel-2024-Q1",
      "fields_added": {
        "name": "Equinix DC11", "operator": "Equinix",
        "lat": 39.04, "lng": -77.48, "state": "VA"
      }
    },
    {
      "timestamp": "2024-08-22T...",
      "change_type": "field_update",
      "source": "DC Hub auto-discovery (PJM queue filing)",
      "fields_changed": {
        "power_mw": {"from": null, "to": 32.0},
        "construction_status": {"from": "planned", "to": "construction"}
      }
    },
    {
      "timestamp": "2025-04-10T...",
      "change_type": "field_update",
      "source": "Equinix Q1 2025 earnings call (manual curation)",
      "fields_changed": {
        "power_mw": {"from": 32.0, "to": 36.0},
        "tenant_known": {"from": null, "to": "primarily hyperscale colocation"}
      }
    },
    {
      "timestamp": "2025-11-30T...",
      "change_type": "field_update",
      "source": "DC Hub auto-discovery (ISO interconnection complete)",
      "fields_changed": {
        "construction_status": {"from": "construction", "to": "operational"}
      }
    }
  ],
  "summary": {
    "first_discovered": "2024-03-15T...",
    "discovery_source": "Baxtel-2024-Q1",
    "last_changed": "2025-11-30T...",
    "total_revisions": 4,
    "fields_with_history": ["power_mw", "construction_status", "tenant_known"]
  }
}
```

### Delivery commitment

**90 days from JSC consensus.** Phase 1: facility-level history (highest-leverage for validation paper). Phase 2: transmission + gas. Phase 3: utility-load forecast-vs-realized rollup.

### Required infrastructure

This requires a `facility_history` (et al.) table writing per-change records on every UPSERT to the canonical tables. DC Hub already logs changes to a `brain_findings` audit table for internal reconciliation — extending this for partner consumption is a natural extension.

### Schedule A amendment text

> **Schedule A.13 — Historical Journey (Entity Lineage)**
>
> Five (5) endpoints providing per-entity version history: chronological log of every field change with timestamp, source attribution, and before/after values. Supports time-travel queries (`?as_of=YYYY-MM-DD`). Includes a unified lineage endpoint across data-centers, transmission, gas, and utility-load entities. Designed for peer-reviewed publication reproducibility — allows validating reVeal projections at time T against operational reality at any subsequent time T+N.

---

## §4 — Combined Schedule A overview after Phase 2

| Schedule | Theme | Endpoints | Status |
|---|---|---|---|
| A.1 Grid + Interconnection | Existing | 6 + 3 additions (r78-e) | ✅ Live |
| A.2 Siting Variables | Existing | 6 | ✅ Live |
| A.3 Composite Intelligence | Existing | 7 | ✅ Live |
| A.4 Market + Facility Data | Existing | 5 + 1 snapshot | ✅ Live |
| A.5 reVeal-Specific | Existing | 6 | ✅ Live |
| A.6 Global Infrastructure | r78-e proposed | 6 | ✅ Live, JSC consensus pending |
| A.7 Site Briefs and Reports | r78-e proposed | 10 | ✅ Live, JSC consensus pending |
| A.8 Methodology and Data Dictionary | r78-e proposed | 2 | ✅ Live, satisfies E.1 |
| A.9 Reports and Exports | r78-e proposed | 5 | ✅ Live, JSC consensus pending |
| A.10 Deal Intelligence | r78-e proposed | 5 | ✅ Live, JSC consensus pending |
| **A.11 Geometry** | **r79 NEW (this doc)** | **6** | **🛠 60-day build** |
| **A.12 Source Attribution and Provenance** | **r79 NEW** | **3** | **🛠 30-day build** |
| **A.13 Historical Journey (Entity Lineage)** | **r79 NEW** | **5** | **🛠 90-day build, phased** |
| | | **66 endpoints** | |

---

## §5 — Build timeline (DC Hub commitments)

Calibrated to JSC review cadence. Each item ships independently — no critical-path dependencies between schedules.

| Schedule | Build window | First-light date target |
|---|---|---|
| A.11 Geometry | 60 days from JSC consensus | 2026-08-08 |
| A.12 Source Attribution / Provenance | 30 days from JSC consensus | 2026-07-09 |
| A.13 Historical Journey — Phase 1 (facilities) | 90 days from JSC consensus | 2026-09-07 |
| A.13 Phase 2 (transmission + gas) | 120 days | 2026-10-07 |
| A.13 Phase 3 (utility-load) | 150 days | 2026-11-06 |

---

## §6 — Open questions for the JSC

1. **Geometry preference** — does reVeal want raw OSM Overpass polygons, or DC Hub-curated (cleaner boundaries, smoother for raster intersection)?
2. **Provenance granularity** — per-field-per-row, per-field-per-category, or both?
3. **Historical journey scope** — full audit log per entity, or rollup snapshots at fixed cadences (daily, weekly)?
4. **Time-travel queries** — `?as_of=YYYY-MM-DD` on existing endpoints, or only via the `/history` endpoints?
5. **Schedule amendment timing** — formal addendum signed at JSC Q1 review (90 days), or rolling addendum as each schedule ships?

---

## §7 — File pointers

- `docs/NLR_UPGRADE_TO_ENTERPRISE.sql` — paste in Neon to run §0 upgrade
- `docs/NLR_PRODUCT_ROADMAP.md` — Day 1 → 24mo timeline (this proposal extends the 90-day milestones)
- `docs/NLR_SCHEDULE_A_EXPANSION.md` — A.6-A.10 (this proposal adds A.11-A.13)
- `docs/NLR_MOU_v1.md` + `.docx` — current MOU (Schedule A amendments target Article XIV change-management process)
- `docs/NLR_PLAYBOOK.md` — reply variants for NLR responses
