# Welcome to DC Hub — NLR Enterprise Edition

**A guide for the NLR reVeal team**
*Updated June 2026*

This guide walks you through everything you need to get value from DC Hub on Day 1 — your portal login, the Land & Power interactive map, and how to interpret the data underneath. Built specifically for NLR's research workflow.

---

## What you have access to

You're enrolled as an **Enterprise-tier** user under the NLR Research Seed engagement. That means:

| | |
|---|---|
| 🗺️ **Land & Power Map** | Interactive map with 20+ live data layers — substations, fiber, water, gas, climate, drought, solar/wind resource, social acceptance |
| 📊 **Site-level reports** | Pre-computed analytical reports for any lat/lon — composite siting score, 2030-2050 deployment forecasts, risk assessment, BUILD / CAUTION / BLOCK verdicts |
| 🌍 **Global infrastructure** | 1,300+ Internet exchanges (IXPs), submarine cables, cable landing points, global power plants, gas networks, hazard overlays |
| 📈 **Market intelligence** | Per-market briefs across 300+ markets, per-state briefs, per-operator profiles, hyperscaler footprint maps |
| 🔌 **Real-time grid data** | Live ISO load + reserve margin + queue depth for all major US ISOs (PJM, ERCOT, CAISO, MISO, ISO-NE, NYISO, SPP) |
| 🛠️ **MCP server** | Conversational data access through Claude, ChatGPT, Cursor, or any MCP-compatible AI tool |
| 📋 **API + Data Dictionary** | Direct programmatic access for reVeal integration, with machine-readable schema documentation |
| 🔍 **Source attribution** | Documented provenance for every data field — where it came from, refresh cadence, license terms |

No daily call caps within reason, no per-feature paywalls, no upsell prompts. Everything is unlocked.

---

## Part 1 — Get into the portal

### Step 1: Visit the login page

Open your browser to:

> **https://dchub.cloud/login**

### Step 2: First-time password setup

Your accounts were created tonight with your @nlr.gov email addresses. To set a password:

1. On the login page, click **"Forgot password?"**
2. Enter your NLR email (e.g., `gabriel.zuckerman@nlr.gov`)
3. Check your inbox — you'll receive a temporary password from `noreply@dchub.cloud` within 1 minute
4. Return to the login page and enter your email + the temp password
5. You'll be prompted to set a permanent password — pick something you'll remember

If the temp email doesn't arrive within 5 minutes:
- Check your spam folder
- Email `partnerships@dchub.cloud` and we'll generate one manually

### Step 3: Confirm your Enterprise status

After login, look at the top-right of any page. You should see a badge that reads **"Enterprise"** (in dark slate). If it shows "Free" or "Developer," click your profile menu → **Refresh tier** and re-load. The badge controls which features are visible.

### Step 4: Verify the API key works (optional, for Ian's integration work)

You can also use the API directly — your API key was sent in a separate email from `jonathan@dchub.cloud`. To smoke-test from a terminal:

```bash
curl -H "X-API-Key: YOUR_KEY" \
  "https://dchub.cloud/api/v1/site-forecast?lat=39.04&lon=-77.48&state=VA"
```

You should see a JSON response with `deployment_forecast` containing both `reference_scenario` and `high_dc_scenario` for years 2030-2050. The `methodology` field will cite `NLR/PR-6A20-99256` — your team's published paper.

---

## Part 2 — The Land & Power Map (your main workspace)

Navigate to:

> **https://dchub.cloud/land-power-map**

This is the visual interface for everything you'd want to know about a potential data-center site, anywhere in the US (and increasingly globally).

### The interface, at a glance

```
┌─────────────────────────────────────────────────────────────────┐
│ Search address or lat,lon              [🔍]  Save site │ Export │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌──────────┐                                                   │
│  │ LAYERS   │                                                   │
│  │ ✓ Grid    │                                                   │
│  │ ✓ Fiber   │              [ THE MAP ]                          │
│  │ ☐ Solar   │                                                   │
│  │ ☐ Water   │       (Click anywhere for a site report)          │
│  │ ☐ Gas     │                                                   │
│  │ ☐ Drought │                                                   │
│  │  ...      │                                                   │
│  │ + Toggle  │                                                   │
│  └──────────┘                                                   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

- **Left sidebar:** layer toggles. Click any layer name to switch it on/off.
- **Search bar (top):** type a street address, a county, a zip code, or coordinates as `lat,lon`. The map flies to that location and zooms appropriately.
- **Click anywhere on the map:** opens a site-report popup with the composite site score, BUILD/CAUTION/BLOCK verdict, and layer-by-layer breakdown for that point.
- **Save site (top-right):** bookmark the current map view + clicked point for later retrieval.

### The layers you'll use most

The layer list is grouped into categories. Here's what each does, in the order you're likely to want them.

#### 🔌 Grid + Power layers

| Layer | What it shows | Source |
|---|---|---|
| **Substations** | All US transmission substations (~79,000 facilities) as clickable points | HIFLD, refreshed quarterly |
| **Transmission lines** | High-voltage transmission corridors | HIFLD |
| **Interconnection queue** | Live ISO interconnection queue facilities — colored by queue stage | PJM/ERCOT/CAISO/etc., refreshed every 20 minutes |
| **Solar resource** | NREL solar PV resource (annual GHI) | NREL PVWatts API, tile-rendered |
| **Wind resource** | NREL wind resource at 100m hub height | NREL Wind Toolkit, tile-rendered |
| **DC Projects (live)** | All known active DC construction/operating sites in the corridor — currently ~155 projects | DC Hub auto-discovery from filings + news, refreshed every 4 hours |

#### 💧 Water layers

| Layer | What it shows | Source |
|---|---|---|
| **USGS water stress** | Per-well USGS water-level measurements; aggregate stress score | USGS direct, live |
| **USDM drought** | US Drought Monitor current-week polygons (5-category severity) | US Drought Monitor ArcGIS service, refreshed weekly |
| **Cooling water risk** | Composite cooling-feasibility score for DC operations | Derived — see §"Derived vs raw" below |

#### 🛰️ Network layers

| Layer | What it shows | Source |
|---|---|---|
| **Fiber routes** | Long-haul fiber routes for 20+ carriers | DC Hub commercial intel |
| **IXPs (1,300+)** | Internet exchanges via PeeringDB (global) | PeeringDB API |
| **Submarine cables** | Submarine cable network + landing points | OSM Overpass + DC Hub curation |

#### ⛽ Gas layers

| Layer | What it shows | Source |
|---|---|---|
| **Gas pipelines** | Natural gas transmission pipelines | EIA |
| **Compressor stations** | Gas compressor stations (clickable, per-facility) | EIA + OSM |
| **Gas-fired generation** | Gas-fired generation facilities + capacity | EIA Form 860 |

#### 🌪️ Climate + risk layers

| Layer | What it shows | Source |
|---|---|---|
| **FEMA hazard** | FEMA hazard zones (flood, hurricane, wildfire) | FEMA HIFLD |
| **Climate risk** | Climate-risk overlay for siting evaluation | Composite — see §"Derived vs raw" |
| **Social acceptance** | Local-opposition signal by county (fills slide-25 gap from your March paper) | DC Hub auto-discovery from public comments + filings |

#### 💵 Economic layers

| Layer | What it shows | Source |
|---|---|---|
| **Tax incentives** | 50-state DC tax abatement programs | DC Hub curated |
| **Energy prices** | EIA state-level retail electricity rates | EIA |
| **DCPI markets** | The 300+ markets in our Data Center Power Index, colored by composite score | DC Hub composite |

### Searching for a specific site

The search bar accepts multiple formats:

| Input | Behavior |
|---|---|
| `21450 Beaumeade Cir, Ashburn, VA` | Geocodes the address, zooms in |
| `Loudoun County, VA` | Zooms to the county boundary |
| `39.04, -77.48` | Treats as lat/lon directly |
| `Ashburn` | Searches for the closest matching place |
| `PJM` | Zooms to the ISO region polygon |

### Clicking on the map — the site-report popup

When you click any point on the map, a popup appears showing:

- **Composite site score** (0-100, with letter grade A-F)
- **Verdict:** BUILD / CAUTION / BLOCK
- **Time to power** estimate (months until interconnection)
- **Component scores** — substation proximity, market density, transmission infrastructure, tax incentives, water risk, social acceptance, climate risk
- **Nearest substation distance** (km)
- **Facility density** within 100 km
- **Methodology citation** — references NLR/PR-6A20-99256

Click "**Open full report**" in the popup to navigate to a dedicated page with the deployment forecast (2030/2035/2040/2050 reference + high-DC scenarios), full layer breakdown, and a comparable-sites table.

### Saving sites for tracking

To bookmark a site:

1. Search for or click on the location
2. Click **"Save site"** in the top-right
3. Add an optional name + notes ("Pilot region candidate 1 — PJM corridor")
4. The site appears in your **Saved Sites** list (accessed from the profile menu)

Saved sites get monitored automatically:
- Composite score changes are emailed weekly
- Material grid events (queue changes, capacity additions) trigger alerts
- You can configure alert thresholds per-site

### Exporting the data

Top-right **"Export"** menu:

- **CSV** — flat file with all layer values for the current map view
- **GeoJSON** — geo-tagged data for use in QGIS, ArcGIS, or geopandas
- **PDF report** — formatted site report (good for sharing with Galen)
- **API copy-paste** — generates the curl command that would return the same data programmatically

---

## Part 3 — Understanding regions vs facilities (polygons vs points)

DC Hub treats geography at two levels — and reVeal will care about both.

### 🟦 Regions = polygons

A **region** is an area: a metro, an ISO territory, a state, a county. Regions are represented as polygons (boundary lines enclosing an area).

| Region type | Example | Polygon source |
|---|---|---|
| Metro | "Northern Virginia (Loudoun-Fairfax)" | DC Hub curation |
| ISO | "PJM", "ERCOT", "CAISO" | ISO official boundaries |
| State | "VA", "TX" | US Census |
| County | "Loudoun County, VA" | US Census TIGER |
| Drought zone | Current-week USDM polygons (5 severity levels) | US Drought Monitor |

When you query a region endpoint, you can request the polygon geometry:

```
GET /api/v1/geometry/region/northern-virginia
```

Returns GeoJSON suitable for direct intersection with reVeal's 5.76 km × 5.76 km grid:

```json
{
  "type": "Feature",
  "geometry": {"type": "Polygon", "coordinates": [[...]]},
  "properties": {
    "slug": "northern-virginia",
    "iso": "PJM",
    "facility_count": 312,
    "dcpi_composite": 87.3,
    "area_km2": 13567
  }
}
```

### 🔴 Facilities = points

A **facility** is a single physical location: a data center, a substation, a gas compressor station, a USGS well. Facilities are represented as points (single lat/lon coordinate).

Click any facility on the map for its full property record. Or query the point geometry directly:

```
GET /api/v1/geometry/facility/<id>
```

```json
{
  "type": "Feature",
  "geometry": {"type": "Point", "coordinates": [-77.48, 39.04]},
  "properties": {
    "id": "dc-12345",
    "name": "Example DC11",
    "operator": "Example Operator",
    "power_mw": 32.0,
    "construction_status": "operational",
    "iso": "PJM",
    "state": "VA",
    "first_discovered": "2024-03-15"
  }
}
```

> 📅 **Note:** GeoJSON-formatted geometry endpoints (Schedule A.11) are launching within 60 days. Today you can extract `lat`/`lon` directly from facility records and build polygons via the regions returned in market briefs.

---

## Part 4 — Where the data comes from (derived vs raw)

For NLR's validation paper, every data point needs a clean provenance chain. DC Hub categorizes every field as either **raw** (directly from a source) or **derived** (computed from one or more raw inputs).

### 🟢 Raw fields — direct from source

These come from a primary source unchanged. We re-format and normalize, but don't compute.

| Category | Field examples | Source |
|---|---|---|
| Facility identity | `name`, `operator`, `lat`, `lng`, `state`, `address` | Baxtel + manual curation |
| Substations | All geometry + `voltage_kv`, `operator` | HIFLD |
| Water | `water_level_ft`, `well_depth_ft`, `aquifer_name` | USGS direct |
| Solar | `ghi_kwh_m2_day`, `dni_kwh_m2_day` | NREL PVWatts |
| Wind | `wind_speed_100m_ms`, `capacity_factor` | NREL Wind Toolkit |
| Gas | Pipeline geometry, compressor station locations | EIA |
| Tax incentives | Per-state abatement program text | DC Hub curated from state revenue depts |
| Energy prices | `retail_rate_cents_per_kwh` | EIA monthly |
| Climate | NOAA temperature + precipitation timeseries | NOAA |

### 🟡 Derived fields — computed from raw

These are calculated, with documented formulas.

| Field | Computed from | Formula / method |
|---|---|---|
| `composite_site_score` (0-100) | All siting layers | Weighted sum, weights documented at `/api/v1/methodology/composite-score` |
| `nearest_substation_km` | facility `lat/lng` + HIFLD substation geometry | Haversine distance to nearest substation point |
| `verdict` (BUILD/CAUTION/BLOCK) | `composite_site_score` | Score ≥75 → BUILD; 50-74 → CAUTION; <50 → BLOCK |
| `time_to_power_months` | ISO queue depth + transmission slack | DC Hub model, documented at `/api/v1/methodology/time-to-power` |
| `power_mw` (for facilities) | Filings + ISO interconnection queue + news | DC Hub auto-discovery with confidence scoring |
| `construction_status` | News + queue events + manual confirmation | State-machine with documented transitions |
| `social_acceptance_index` | Public comments + filings + permitting records | DC Hub NLP pipeline |
| `2050_forecast_mw` (reference + high-DC) | EER (Jones et al. 2024) ADP + DC Hub composite | Linear scaling on top of EER projections |

### 📋 The Data Dictionary — your full reference

The complete, machine-readable provenance for every field:

> **https://dchub.cloud/api/v1/methodology/data-dictionary.json**

This is the canonical source for the validation paper's Methods section. It includes:
- Schema (field types + constraints) per endpoint
- Upstream source per field
- Refresh cadence per source
- Known limitations + caveats
- License terms per source

For peer-reviewed publication, every field you cite should resolve to a documented entry in this dictionary.

---

## Part 5 — Tracking changes over time (history)

reVeal's validation rig will compare projections-at-time-T to operational-reality-at-time-T+N. To do that, you need to see how DC Hub's records of a facility (or grid layer) changed over time.

### What's available today

Every facility record carries timestamps that let you understand its journey:

| Field | What it tells you |
|---|---|
| `first_discovered` | When DC Hub first saw this facility (date + source) |
| `last_updated` | When any field on this record last changed |
| `power_mw_revisions` | How many times the power capacity has been revised |
| `confidence_score` | DC Hub's confidence in the current record (0-100, recomputed nightly) |
| `verification_status` | Auto-discovery only / Manually verified / Operator-confirmed |

Click any facility in the map → "**Show history**" tab → see a chronological log of every field change with source attribution and confidence scoring.

### What's launching within 90 days (Schedule A.13)

Full per-entity historical journey endpoints:

```
GET /api/v1/facility/<id>/history
GET /api/v1/transmission/<id>/history
GET /api/v1/gas/<facility_id>/history
GET /api/v1/utility-load/<iso>/history?from=2024-01&to=2026-06
GET /api/v1/lineage/<entity_type>/<entity_id>
```

Each returns a chronological log of every field change:

```json
{
  "facility_id": "dc-12345",
  "history": [
    {
      "timestamp": "2024-03-15T...",
      "change_type": "discovery",
      "source": "Baxtel-2024-Q1",
      "fields_added": {"power_mw": null, "construction_status": "planned"}
    },
    {
      "timestamp": "2024-08-22T...",
      "change_type": "field_update",
      "source": "PJM interconnection queue Q2 filing",
      "fields_changed": {
        "power_mw": {"from": null, "to": 32.0},
        "construction_status": {"from": "planned", "to": "construction"}
      }
    },
    ...
  ]
}
```

Plus **time-travel queries** on existing endpoints — append `?as_of=2024-12-01` to any data endpoint and DC Hub returns the state of the world as of that date.

This is the cleanest scaffolding for the reVeal Validation Study — projected at T, validated against operational reality at T+N for any T.

---

## Part 6 — Conversational access via MCP (optional, for Galen)

If you use Claude.ai, ChatGPT (with custom connector support), Cursor, or any other MCP-compatible AI assistant, you can talk to DC Hub directly.

### Setup (Claude.ai example)

1. Open Claude.ai → **Settings → Connectors**
2. Click **"Add custom connector"**
3. Fill in:
   - **Name:** DC Hub
   - **URL:** `https://dchub.cloud/mcp`
   - **Authentication:** API key (paste your key)
4. Save

DC Hub now appears as a tool in Claude. Ask:

> *"Pull DC Hub's site-forecast for lat 39.04 lon -77.48 in Virginia, then compare the reference vs high-DC 2050 scenarios."*

Claude calls `get_site_forecast`, presents both scenarios with the methodology citation, and lets you follow up with comparison questions naturally.

This is especially useful for ad-hoc methodology exploration during paper-writing.

---

## Part 7 — Where to go from here

### Quick wins (today)

- [ ] Log into the portal at `dchub.cloud/login` and reset your password
- [ ] Open the Land & Power Map and explore the Ashburn, VA area (default view)
- [ ] Save 3-5 candidate sites for the pilot region (PJM or ERCOT corridor)
- [ ] Run one smoke-test API call from your terminal

### This week

- [ ] Bookmark `/api/v1/methodology/data-dictionary.json` for the validation paper Methods section
- [ ] If Galen uses Claude, set up the MCP custom connector
- [ ] If Ian's writing reVeal-integration code, the technical handbook at `docs/NLR_REVEAL_INTEGRATION_GUIDE.md` has working code samples for each integration pattern

### Within 30 days

- [ ] JSC kickoff call (scheduled separately) — pilot region + priority limitation confirmed
- [ ] First validation-rig prototype using `/site-forecast` + reVeal output side-by-side on the pilot region
- [ ] Saved-site alerts configured for your tracking sites

### Within 90 days

- [ ] GeoJSON geometry endpoints (Schedule A.11) live
- [ ] Source-attribution provenance endpoints (A.12) live
- [ ] Historical-journey endpoints (A.13) Phase 1 live
- [ ] First Validation Study findings memo drafted

---

## Frequently asked questions

**Q: My password reset email didn't arrive.**
A: Check spam, then email `partnerships@dchub.cloud`. We'll generate one manually within 1 business hour.

**Q: Can I share my API key with a colleague at NLR?**
A: Per the MOU, API keys are individually bound. If you need access for additional NLR researchers, email `partnerships@dchub.cloud` and we'll mint them a key under the NLR Research Seed engagement (no additional fee).

**Q: What's the difference between the Land & Power Map and the API?**
A: Same data, different access patterns. The map is for visual exploration + sharing screenshots. The API is for programmatic integration into reVeal. Both serve identical data — choose whichever fits the task.

**Q: How do I cite DC Hub in the Validation Study?**
A: Per MOU Schedule D, the canonical attribution is *"Data provided by DC Hub (dchub.cloud) under a research license to [NLR Operating Entity Legal Name]."* For citation in the bibliography: *"Martone, J. et al. DC Hub: Real-time data-center operational intelligence platform. dchub.cloud, 2026."*

**Q: My calls return HTTP 429.**
A: You've hit the rate limit (200 requests/sec sustained, 10M/month aggregate). Wait the duration specified in the `Retry-After` header. If you're consistently hitting limits, email us and we'll raise them — your tier doesn't have a hard cap.

**Q: Can I download bulk data for offline analysis?**
A: Yes. Use `/api/v1/exports/build` to kick off a bulk export job, or use the async grid-export endpoints for full-region exports. For ad-hoc downloads, every map view has an "Export" button that produces CSV or GeoJSON.

**Q: What if I find an error in the data?**
A: Email `partnerships@dchub.cloud` with the facility ID + the field that's wrong + your source for the correction. We update within 1 business day and you'll see the change reflected in the next refresh cycle.

**Q: Does DC Hub work for international markets?**
A: Yes, increasingly so. The 300+ market DCPI is global. The Land & Power Map's `/global-*` layers cover infrastructure outside the US (IXPs, submarine cables, gas networks, hazard data). International coverage continues to grow.

**Q: How do I see what's new in DC Hub?**
A: We maintain a changelog at `dchub.cloud/changelog`. Material changes to your licensed endpoint surface are also summarized in the monthly NLR briefing email.

---

## Need help

| For | Contact |
|---|---|
| **Technical questions / API integration** | `jonathan@dchub.cloud` (Jonathan Martone, JSC technical lead) |
| **Account / login / billing** | `partnerships@dchub.cloud` |
| **Data quality reports** | `partnerships@dchub.cloud` |
| **MCP server issues** | `jonathan@dchub.cloud` |
| **Slack/Discord channel** | Can stand up dedicated NLR channel on request |
| **Pair programming session** | Email Jonathan for a screen-share — usually same-day for NLR |

---

## Quick-reference URLs

| | |
|---|---|
| Portal login | `https://dchub.cloud/login` |
| Land & Power Map | `https://dchub.cloud/land-power-map` |
| Your saved sites | `https://dchub.cloud/saved-sites` |
| Data dictionary | `https://dchub.cloud/api/v1/methodology/data-dictionary.json` |
| OpenAPI spec | `https://dchub.cloud/openapi.json` |
| MCP server | `https://dchub.cloud/mcp` |
| Status page | `https://dchub.cloud/status` |
| API health check | `https://dchub.cloud/healthz` |
| Changelog | `https://dchub.cloud/changelog` |
| NLR partner page | `https://dchub.cloud/partners/nlr` |

---

*Welcome aboard. We're excited to support the reVeal Validation Study — and to learn from your team's research as much as you'll be learning from our data. — DC Hub*
