# DC Hub × NLR — reVeal Integration Guide

**Version 1 — 2026-06-09**
**For:** NLR JSC + reVeal integration team (Galen, Ian)
**Status:** Ready to present + execute

Concrete, runnable patterns for integrating DC Hub data into NLR's reVeal model. Written assuming reVeal's published methodology (`NLR/PR-6A20-99256`, March 2026, pages 23-24) — 5.76 km × 5.76 km grid resolution, 88% utilization factor, Baxtel 2025 initial sites, EER (Jones et al. 2024) high-DC load projections through 2050.

---

## §1 — What this guide covers

Three integration patterns, ordered by build effort:

| Pattern | Effort | Outcome |
|---|---|---|
| **A. Drop-in layer replacement** | < 1 day | Replace any single reVeal input layer with DC Hub live data |
| **B. Side-by-side validation rig** | 3-5 days | Run reVeal alongside DC Hub composite, compare cell-by-cell |
| **C. Tight loop integration** | 2-3 weeks | reVeal's training/inference pipeline pulls from DC Hub directly per cell |

All three patterns work today with your existing Developer keys (now upgraded to Enterprise tier per the SQL we ran). No additional gating, no rate limits within reason, no extra setup.

---

## §2 — Quickstart (5 minutes)

### 2.1 Confirm your key works

```bash
curl -H "X-API-Key: $YOUR_KEY" \
  "https://dchub.cloud/api/v1/site-forecast?lat=39.04&lon=-77.48&state=VA"
```

Expected: JSON response with `deployment_forecast` containing both `reference_scenario` and `high_dc_scenario` for years 2030/2035/2040/2050. The `methodology` field cites `NLR/PR-6A20-99256` — your team's paper.

### 2.2 Inspect the data dictionary

```bash
curl -H "X-API-Key: $YOUR_KEY" \
  "https://dchub.cloud/api/v1/methodology/data-dictionary.json"
```

Machine-readable JSON Schema covering schema, upstream source, refresh cadence, known limitations for every Schedule A endpoint. Use this when you write the validation paper's Methods section.

### 2.3 Try the MCP server (optional, for Claude/Cursor/ChatGPT users)

```
https://dchub.cloud/mcp
```

Server card at `/.well-known/mcp/server-card.json`. If Galen uses Claude or ChatGPT for ad-hoc methodology exploration, configuring the DC Hub MCP custom connector lets you query DC Hub conversationally — e.g., *"compare interconnection queue depth in PJM vs ERCOT for the past 12 months."*

---

## §3 — Pattern A: Drop-in layer replacement

Use this when you want to swap one reVeal input layer for live DC Hub data without touching the rest of the model. Highest ROI is the **transmission hosting capacity** layer — your March 2026 paper explicitly flagged this as the #1 priority improvement.

### A.1 — Transmission hosting capacity (reVeal's #1 flagged gap)

reVeal currently uses a proxy for transmission hosting capacity. DC Hub has live data per ISO.

```python
import requests

DCHUB_KEY = "dchub_developer_..."  # your enterprise key
HEADERS = {"X-API-Key": DCHUB_KEY}

def get_transmission_hosting_capacity(iso_code):
    """Returns dict with reserve_margin, queue_depth, and per-region details."""
    r = requests.get(
        f"https://dchub.cloud/api/v1/grid-intelligence",
        params={"iso": iso_code},
        headers=HEADERS,
    )
    r.raise_for_status()
    return r.json()

# Per reVeal's case study: PJM region
pjm = get_transmission_hosting_capacity("PJM")
print(f"Reserve margin: {pjm['reserve_margin']}")
print(f"Queue depth: {pjm['queue_depth']}")
```

**reVeal integration:** in `reveal/grid_layer.py` (or wherever the grid feature is computed), replace the proxy with this live signal. The response includes per-region breakdown matching reVeal's 5.76km grid via the `region_polygons` field when you also include the geometry parameter (planned A.11, see §6).

### A.2 — Water availability layer (reVeal's #3 flagged gap)

```python
def get_water_stress_for_cell(lat, lon):
    """USGS water-stress readings near a cell centroid."""
    r = requests.get(
        f"https://dchub.cloud/api/v1/water/stress",
        params={"lat": lat, "lon": lon},
        headers=HEADERS,
    )
    return r.json()

# For each reVeal cell centroid, swap in DC Hub water data
for cell_id, (lat, lon) in reveal_cells.items():
    water = get_water_stress_for_cell(lat, lon)
    cell.water_layer = water  # replaces reVeal's existing water proxy
```

**Returns:** ~15 KB of well-level USGS measurement data per call. Includes water_level_date, well_depth, aquifer name when known.

### A.3 — Social acceptance layer (reVeal's #4 flagged gap)

Your March 2026 paper specifically flagged social acceptance as data-quality-limited. We built `/social-acceptance-index` to fill exactly this gap.

```python
def get_social_acceptance_for_county(state, county):
    r = requests.get(
        f"https://dchub.cloud/api/v1/social-acceptance-index",
        params={"state": state, "county": county},
        headers=HEADERS,
    )
    return r.json()

# Returns: opposition_index (0-100), recent_filings, public_comment_volume
loudoun = get_social_acceptance_for_county("VA", "Loudoun")
```

### A.4 — Interconnection queue depth (reVeal's #2 flagged gap)

```python
def get_interconnection_queue(iso_code):
    r = requests.get(
        f"https://dchub.cloud/api/v1/interconnection-queue",
        params={"iso": iso_code},
        headers=HEADERS,
    )
    return r.json()

# Returns: queue_count, total_queued_mw, avg_queue_age_months,
# per-status breakdown (queued, studies-in-progress, withdrawn, in-service)
queue = get_interconnection_queue("ERCOT")
```

---

## §4 — Pattern B: Side-by-side validation rig

Use this to run reVeal and DC Hub composite scoring against the same cells, then compare residuals. This is the natural scaffolding for the Validation Study (MOU Stream B).

### B.1 — The validation loop

```python
import requests
import pandas as pd

DCHUB_KEY = "dchub_developer_..."
HEADERS = {"X-API-Key": DCHUB_KEY}

def reveal_score(cell_id):
    """reVeal's existing composite for a cell."""
    # NLR's existing reveal.composite_score(cell_id) function
    return reveal.composite_score(cell_id)

def dchub_score(lat, lon, state):
    """DC Hub's composite for the same cell."""
    r = requests.get(
        f"https://dchub.cloud/api/v1/site-forecast",
        params={"lat": lat, "lon": lon, "state": state},
        headers=HEADERS,
    )
    return r.json()["suitability"]["composite_score"]

# Iterate over reVeal's cells (e.g., PJM corridor pilot region)
results = []
for cell_id, (lat, lon, state) in reveal.cells_for_region("PJM").items():
    results.append({
        "cell_id": cell_id,
        "lat": lat,
        "lon": lon,
        "reveal_score": reveal_score(cell_id),
        "dchub_score": dchub_score(lat, lon, state),
    })

df = pd.DataFrame(results)
df["residual"] = df["dchub_score"] - df["reveal_score"]
df["abs_residual"] = df["residual"].abs()

# Top-10 cells where the two models disagree most
print(df.nlargest(10, "abs_residual"))
```

### B.2 — Bulk validation via reveal-cell-bulk

For full-region validation, use the bulk endpoint:

```python
def get_reveal_cells_bulk(bbox):
    """bbox = (min_lat, min_lon, max_lat, max_lon)"""
    r = requests.get(
        f"https://dchub.cloud/api/v1/reveal-cell-bulk",
        params={"bbox": f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}"},
        headers=HEADERS,
    )
    return r.json()

# PJM Mid-Atlantic corridor: roughly 38°N to 42°N, -80°W to -74°W
mid_atlantic = get_reveal_cells_bulk((38.0, -80.0, 42.0, -74.0))
# Returns: list of cells, each with DC Hub composite + 6 layer scores
```

### B.3 — Async grid export (for full-CONUS regions)

```python
import time

def kickoff_grid_export(region):
    r = requests.post(
        f"https://dchub.cloud/api/v1/reveal-grid-export",
        json={"region": region},
        headers=HEADERS,
    )
    return r.json()["job_id"]

def poll_until_done(job_id, timeout_sec=600):
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        r = requests.get(
            f"https://dchub.cloud/api/v1/reveal-grid-export/status/{job_id}",
            headers=HEADERS,
        )
        result = r.json()
        if result["status"] == "complete":
            return result["data_url"]
        time.sleep(5)
    raise TimeoutError(f"Job {job_id} did not complete in {timeout_sec}s")

job = kickoff_grid_export("PJM")
data_url = poll_until_done(job)
# Download from data_url (presigned S3 URL, 24h validity)
```

### B.4 — Validation paper scaffolding

We'd recommend tracking the following in a CSV alongside your existing reVeal output, per cell:

| Column | Source | Type |
|---|---|---|
| `cell_id` | reVeal | reVeal cell identifier |
| `lat`, `lon` | reVeal | cell centroid |
| `state`, `iso` | derived | mapping from lat/lon |
| `reveal_score` | reVeal composite | float 0-100 |
| `reveal_high_dc_2050` | reVeal projection | float MW |
| `dchub_composite` | DC Hub site-forecast | float 0-100 |
| `dchub_high_dc_2050` | DC Hub site-forecast | float MW |
| `dchub_reference_2050` | DC Hub site-forecast | float MW |
| `dchub_transmission_hosting` | DC Hub grid-intelligence | float reserve margin |
| `dchub_water_stress` | DC Hub water/stress | float aggregate |
| `dchub_social_acceptance` | DC Hub social-acceptance-index | float opposition |

This becomes Tables 1-3 in the Validation Study paper. We provide a starter pyspark notebook on request.

---

## §5 — Pattern C: Tight loop integration

For full integration into reVeal's training/inference pipeline. Caches DC Hub responses for performance, refreshes per a configurable cadence.

### C.1 — Client wrapper with caching

```python
# reveal/integrations/dchub_client.py
import requests
import functools
import os
import time

DCHUB_KEY = os.environ["DCHUB_KEY"]
DCHUB_BASE = "https://dchub.cloud"
CACHE_TTL_SECONDS = 3600  # 1 hour — match DC Hub's edge cache

class DCHubClient:
    def __init__(self, api_key=DCHUB_KEY):
        self.session = requests.Session()
        self.session.headers["X-API-Key"] = api_key
        self.cache = {}

    @functools.lru_cache(maxsize=10000)
    def site_forecast(self, lat, lon, state):
        """Returns site-forecast for a cell. Cached for the worker's lifetime."""
        r = self.session.get(
            f"{DCHUB_BASE}/api/v1/site-forecast",
            params={"lat": lat, "lon": lon, "state": state},
        )
        r.raise_for_status()
        return r.json()

    def grid_intelligence(self, iso):
        return self.session.get(
            f"{DCHUB_BASE}/api/v1/grid-intelligence",
            params={"iso": iso},
        ).json()

    def water_stress(self, lat, lon):
        return self.session.get(
            f"{DCHUB_BASE}/api/v1/water/stress",
            params={"lat": lat, "lon": lon},
        ).json()

    def social_acceptance(self, state, county):
        return self.session.get(
            f"{DCHUB_BASE}/api/v1/social-acceptance-index",
            params={"state": state, "county": county},
        ).json()

    # Async bulk operations
    def kickoff_grid_export(self, region):
        return self.session.post(
            f"{DCHUB_BASE}/api/v1/reveal-grid-export",
            json={"region": region},
        ).json()["job_id"]

    def grid_export_status(self, job_id):
        return self.session.get(
            f"{DCHUB_BASE}/api/v1/reveal-grid-export/status/{job_id}",
        ).json()
```

### C.2 — reVeal feature builder using DC Hub layers

```python
# reveal/features.py
from reveal.integrations.dchub_client import DCHubClient

dchub = DCHubClient()

def build_features_for_cell(cell):
    """Build the feature vector for a reVeal cell using DC Hub data."""
    forecast = dchub.site_forecast(cell.lat, cell.lon, cell.state)
    grid = dchub.grid_intelligence(cell.iso)
    water = dchub.water_stress(cell.lat, cell.lon)
    social = dchub.social_acceptance(cell.state, cell.county)

    return {
        "transmission_hosting_capacity": grid["reserve_margin"],
        "queue_depth": grid["queue_depth"],
        "water_stress_score": water["aggregate_score"],
        "social_acceptance_index": social["opposition_index"],
        "dchub_composite_score": forecast["suitability"]["composite_score"],
        "dchub_reference_2050_mw": forecast["deployment_forecast"]["reference_scenario"]["2050"],
        "dchub_high_dc_2050_mw": forecast["deployment_forecast"]["high_dc_scenario"]["2050"],
    }
```

### C.3 — Batch processing for full-grid runs

For a full PJM or ERCOT run (millions of cells), the per-cell HTTP overhead becomes the bottleneck. Use the bulk endpoint and async grid export:

```python
def process_region(region_code, output_path):
    """Run a full-region grid build."""
    job_id = dchub.kickoff_grid_export(region_code)
    
    # Poll until done (typically 2-10 min for full PJM/ERCOT)
    while True:
        status = dchub.grid_export_status(job_id)
        if status["status"] == "complete":
            # Download the presigned S3 URL
            data = requests.get(status["data_url"]).json()
            with open(output_path, "w") as f:
                json.dump(data, f)
            return data
        time.sleep(5)
```

---

## §6 — Coming features that will further simplify integration

DC Hub will deliver three more schedule-level additions in the next 90 days specifically calibrated to reVeal's research workflow (see `NLR_PHASE_2_PROPOSAL.md`):

### A.11 — Geometry endpoints (60 days)

Returns native GeoJSON instead of lat/lon scalars. Will let reVeal consume DC Hub regions and facilities directly with `geopandas` / `shapely`:

```python
import geopandas as gpd
import json

# Polygon for the Northern Virginia metro
r = requests.get(
    "https://dchub.cloud/api/v1/geometry/region/northern-virginia",
    headers=HEADERS,
)
nv_polygon = gpd.GeoDataFrame.from_features([r.json()])

# All facilities in a bounding box
r = requests.get(
    "https://dchub.cloud/api/v1/geometry/facility/by-bbox",
    params={"bbox": "38.5,-78.0,39.5,-77.0"},
    headers=HEADERS,
)
facilities = gpd.GeoDataFrame.from_features(r.json()["features"])

# Intersect reVeal's 5.76km grid with NV metro polygon
nv_cells = reveal.grid.intersect(nv_polygon)
```

### A.12 — Source attribution + provenance (30 days)

Per-category data lineage documentation. Used directly in the Validation Study's Methods section:

```python
provenance = requests.get(
    "https://dchub.cloud/api/v1/provenance/data-centers",
    headers=HEADERS,
).json()

# Returns: sources (Baxtel, HIFLD, EIA, OSM, DC Hub auto-discovery),
# fields_provided per source, refresh cadence, derived-fields map
```

### A.13 — Historical journey / entity lineage (90 days, phased)

Per-entity version history. Allows validation at time T vs operational reality at time T+N:

```python
history = requests.get(
    f"https://dchub.cloud/api/v1/facility/{facility_id}/history",
    headers=HEADERS,
).json()

# Returns: timeline of every field change with timestamp + source
# Time-travel query: ?as_of=2024-12-01 returns the state as of that date
```

---

## §7 — Authentication, rate limits, error handling

### 7.1 — Authentication

All requests use the `X-API-Key` header (NOT a Bearer token). Your key is bound to your NLR email and your Enterprise tier:

```python
HEADERS = {"X-API-Key": "dchub_developer_..."}
```

### 7.2 — Rate limits

Your Enterprise tier under the NLR Research Seed engagement:
- **200 requests per second** sustained
- **10 million requests per month** aggregate
- **4 full-CONUS grid-export operations per month**

If you exceed these, DC Hub returns HTTP 429 with a `Retry-After` header. We monitor your usage and will reach out before any hard limit.

### 7.3 — Error handling

```python
from requests.exceptions import HTTPError

def get_with_retry(url, params, max_retries=3):
    for attempt in range(max_retries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r.json()
        except HTTPError as e:
            if e.response.status_code == 429:
                # Rate limited
                wait = int(e.response.headers.get("Retry-After", 5))
                time.sleep(wait)
                continue
            elif e.response.status_code == 401:
                raise RuntimeError("Auth failure — check your X-API-Key")
            elif e.response.status_code >= 500:
                time.sleep(2 ** attempt)  # exponential backoff
                continue
            raise
    raise RuntimeError(f"Max retries exhausted for {url}")
```

### 7.4 — Endpoint deprecation policy

Per MOU Schedule G, DC Hub provides:
- **90 days advance notice** of any breaking change
- **180 days advance notice** of full endpoint deprecation
- **12 months no breaking change** for any endpoint cited in your peer-reviewed publication (from journal submission date)

In practice, this means once you cite a DC Hub endpoint in the Validation Study, its schema is locked for at least a year.

---

## §8 — Reference: complete Schedule A endpoint map

Available to your Enterprise-tier key today:

### A.1 Grid + Interconnection (6 + 3 new)

```
GET /api/v1/grid-headroom?iso=<ISO>
GET /api/v1/grid-intelligence?iso=<ISO>
GET /api/v1/grid-data?iso=<ISO>           [alias: /grid/data]
GET /api/v1/interconnection-queue?iso=<ISO>
GET /api/v1/infrastructure
GET /api/v1/energy-prices?state=<S>       [alias: /energy/retail]
GET /api/v1/grid-transition/radar          [new — early-warning radar]
GET /api/v1/ercot/realtime                 [new — ERCOT real-time]
GET /api/v1/grid/intelligence/<region>     [new — per-region]
```

### A.2 Siting Variables (6)

```
GET /api/v1/air-permitting?state=<S>
GET /api/v1/tax-incentives?state=<S>
GET /api/v1/water-risk?lat=<L>&lon=<L>     [alias: /water/stress]
GET /api/v1/fiber-intel?lat=<L>&lon=<L>    [alias: /fiber/intel]
GET /api/v1/renewable-energy?state=<S>     [alias: /energy/renewable]
GET /api/v1/geothermal-potential?lat=<L>&lon=<L>
```

### A.3 Composite Intelligence (7)

```
GET /api/v1/reveal-cell?lat=<L>&lon=<L>
GET /api/v1/colocation-score?lat=<L>&lon=<L>
GET /api/v1/microgrid-viability?lat=<L>&lon=<L>
GET /api/v1/intelligence-index?market=<M>
GET /api/v1/analyze-site?lat=<L>&lon=<L>
POST /api/v1/compare-sites
GET /api/v1/dchub-recommendation?lat=<L>&lon=<L>
```

### A.4 Market + Facility (5 + 1)

```
GET /api/v1/facility?id=<ID>
GET /api/v1/search-facilities?q=<Q>
GET /api/v1/pipeline?state=<S>
GET /api/v1/market-intel?slug=<S>
GET /api/v1/news?facility=<ID>
GET /api/v1/list-transactions
```

### A.5 reVeal-Specific (6)

<!-- Signatures corrected 2026-08-01 (PR #2080). Five of the seven lines here
     did not match the live handlers: reveal-cell-bulk and social-acceptance-index
     and carbon-intensity all 400'd as printed, reveal-grid-export was documented
     as POST against a GET-only route (405), and reveal-validation-feed?region=
     returned 200 while SILENTLY IGNORING region — a partner filtering by region
     received unfiltered global data with no error. Every line below was executed
     against production before being written. Re-verify before editing. -->

```
GET /api/v1/reveal-cell-bulk?min_lat=<L>&max_lat=<L>&min_lon=<L>&max_lon=<L>[&cell_size_km=5][&state=<S>]
GET /api/v1/reveal-grid-export?state=<S>[&format=parquet|geojson|csv]
GET /api/v1/reveal-grid-export?min_lat=<L>&max_lat=<L>&min_lon=<L>&max_lon=<L>[&format=…]
GET /api/v1/reveal-grid-export/status/<job_id>
GET /api/v1/reveal-validation-feed[?since=<ISO-8601>][&status=<CSV>][&projection_year=<YYYY>][&limit=<N≤5000>]
GET /api/v1/social-acceptance-index?lat=<L>&lon=<L>[&radius_km=50]
GET /api/v1/climate-risk?lat=<L>&lon=<L>
GET /api/v1/carbon-intensity?lat=<L>&lon=<L>[&state=<S>]
```

Notes on the corrections:

- **`reveal-cell-bulk` takes four discrete bounds, not a combined `bbox=`.** A
  `bbox=` call returns 400.
- **Keep the cell-bulk extent small — much smaller than the advertised cap.**
  The handler rejects >2,500 cells with a 413, but that ceiling is not reachable
  through `dchub.cloud`. Cost scales with cell count and cold requests are far
  slower than warm ones. Measured 2026-08-01:

  | Extent | Cells | Result |
  |---|---|---|
  | 0.2° × 0.2° | 20 | 200 in 3–6 s |
  | 0.5° × 0.5° | 108 | 200 in 23 s cold, 3 s warm |
  | 1.0° × 1.0° | 414 | **503 at the edge after 25 s** (85 s at the origin) |

  Tile large areas into ~0.2°–0.5° requests rather than issuing one wide call.
- **`reveal-grid-export` is GET, not POST** — a POST returns 405. It has two
  modes: `state=` (pre-rendered) and a bbox quad (queued).
- **`reveal-validation-feed` has no `region` parameter.** It filters on `since`
  (an ISO-8601 date, matched against `first_seen`), `status`, `projection_year`
  and `limit`. Passing `region=` is accepted and ignored, so a region-scoped
  integration would silently receive global rows.
- **`social-acceptance-index` and `carbon-intensity` are keyed on `lat`/`lon`,**
  not `state`/`county` or `region`. `carbon-intensity` accepts an optional
  `state` override; otherwise it derives state from the coordinates.
- `announcement_date` in the validation feed is **always null** — that field has
  no source column on `discovered_facilities`. Bucket on `expected_completion`
  (sparse) or on `first_seen`, and read the `date_basis` and `field_coverage`
  blocks in the response before treating any field as populated.

### A.6 Global Infrastructure (6 NEW since MOU)

```
GET /api/v1/infrastructure/global-gas
GET /api/v1/infrastructure/global-hazards
GET /api/v1/infrastructure/global-ixps        [1,300+ IXPs via PeeringDB]
GET /api/v1/infrastructure/global-power-plants
GET /api/v1/infrastructure/submarine-cables
GET /api/v1/cable-landing-points
```

### A.7 Site Briefs and Reports (10 NEW)

```
GET /api/v1/site-report?lat=<L>&lon=<L>
GET /api/v1/site-report/portal
GET /api/v1/site/value?lat=<L>&lon=<L>
GET /api/v1/site/value/methodology
GET /api/v1/site-selection/canvas
GET /api/v1/state-brief/<state>
GET /api/v1/market-brief/<slug>
GET /api/v1/market-brief/all
GET /api/v1/operator-brief/<slug>
GET /api/v1/hyperscaler-brief/<slug>
```

### A.8 Methodology + Data Dictionary (2 NEW, satisfies Schedule E.1)

```
GET /api/v1/methodology
GET /api/v1/methodology/data-dictionary.json
```

### A.9 Reports + Exports (5 NEW)

```
GET /api/v1/reports/pipeline
GET /api/v1/reports/quarterly/<quarter>.csv
GET /api/v1/reports/quarterly/<quarter>.json
GET /api/v1/reports/state-of-power
POST /api/v1/exports/build
```

### A.10 Deal Intelligence (5 NEW)

```
GET /api/v1/deal-autopsy?deal_id=<ID>
GET /api/v1/dcgi/scores
GET /api/v1/dcgi/scores/<state>
GET /api/v1/dcpi/snapshot
GET /api/v1/dcpi/total
```

---

## §9 — MCP server integration (for Galen's ad-hoc exploration)

If Galen uses Claude.ai or ChatGPT for research, DC Hub's MCP server provides conversational access to the same endpoints.

### 9.1 — Configure Claude custom connector

In Claude.ai → Settings → Custom Connectors → Add new:
- **Name:** DC Hub
- **URL:** `https://dchub.cloud/mcp`
- **Authentication:** API key (your existing key)

### 9.2 — Example conversation

> *"Pull DC Hub's site-forecast for a cell at lat 39.04 lon -77.48 in Virginia, then compare the reference vs high-DC 2050 scenarios."*

Claude calls `get_site_forecast` and presents both scenarios with the source citation.

> *"What's the interconnection queue depth in ERCOT today vs 12 months ago?"*

Claude calls the queue endpoint with appropriate parameters, computes the delta.

### 9.3 — Programmatic MCP

```python
# For Python integration with the MCP server (e.g. in reveal's ML pipeline)
from mcp.client import MCPClient

mcp = MCPClient("https://dchub.cloud/mcp", api_key="dchub_developer_...")

# Tool discovery
tools = mcp.list_tools()  # Returns 30+ tools

# Invoke a tool
result = mcp.invoke_tool("get_site_forecast",
                         params={"lat": 39.04, "lon": -77.48, "state": "VA"})
```

---

## §10 — Performance optimization

### 10.1 — Caching layer

DC Hub edge-caches most read endpoints for 1 hour. To avoid re-fetching:

```python
import requests_cache

# Install with: pip install requests-cache
session = requests_cache.CachedSession("dchub_cache", expire_after=3600)
session.headers["X-API-Key"] = DCHUB_KEY

# Now session.get() responses are cached locally
forecast = session.get(
    "https://dchub.cloud/api/v1/site-forecast",
    params={"lat": 39.04, "lon": -77.48, "state": "VA"},
).json()
```

### 10.2 — Parallel requests (for batch processing)

```python
from concurrent.futures import ThreadPoolExecutor, as_completed

def fetch_one(cell):
    r = requests.get(
        "https://dchub.cloud/api/v1/site-forecast",
        params={"lat": cell.lat, "lon": cell.lon, "state": cell.state},
        headers=HEADERS,
    )
    return cell.id, r.json()

with ThreadPoolExecutor(max_workers=20) as executor:
    futures = {executor.submit(fetch_one, c): c for c in reveal_cells}
    results = {}
    for f in as_completed(futures):
        cell_id, data = f.result()
        results[cell_id] = data
```

20 workers comfortably fits within your Enterprise tier's 200 rps limit.

### 10.3 — When to use bulk endpoints vs per-cell

| Cells in scope | Use |
|---|---|
| < 100 | Per-cell `/site-forecast` is fine |
| 100 - 10,000 | `/reveal-cell-bulk` with bounding box |
| 10,000+ | Async `/reveal-grid-export` job |

---

## §11 — Sample notebooks (deliverable on request)

DC Hub maintains four example Jupyter notebooks that we can share:

1. **`validation_rig_starter.ipynb`** — Pattern B, side-by-side validation for a PJM corridor sample
2. **`reveal_layer_swap.ipynb`** — Pattern A, swap reVeal's water layer with DC Hub's `/water/stress`
3. **`bulk_export_to_geodataframe.ipynb`** — async grid export → geopandas DataFrame
4. **`mcp_methodology_exploration.ipynb`** — Pattern C, MCP-driven exploration in Jupyter via the `mcp` Python client

Email Jonathan to request — we'll send a zip with all four + `requirements.txt`.

---

## §12 — Questions for Galen + Ian to drive the integration

For the JSC kickoff call tomorrow:

1. **Which pattern is the right starting point?** (A: drop-in layer swap, B: validation rig, C: tight loop)
2. **Which reVeal layer is the highest-leverage swap?** Our recommendation: transmission hosting capacity (your March deck's #1 priority).
3. **What's the pilot region for Stream A integration?** Our recommendation: PJM (Mid-Atlantic corridor) + ERCOT (Texas Triangle).
4. **Do you want DC Hub to publish the sample notebooks at `github.com/azmartone67/dchub-revealkit`?** This becomes the Stream C scaffold once the MOU executes.
5. **Should we expose a `/api/v1/reveal-projected-buildout` endpoint that returns reVeal's projections back via DC Hub's MCP?** Reciprocal feed — gives NLR's research massive distribution lift across Claude, ChatGPT, Perplexity users.

---

## §13 — File pointers

- `docs/NLR_PRODUCT_ROADMAP.md` — overall timeline (this guide implements §90-day milestones)
- `docs/NLR_SCHEDULE_A_EXPANSION.md` — the 27 new endpoints referenced throughout
- `docs/NLR_PHASE_2_PROPOSAL.md` — A.11/A.12/A.13 details (60-90 day deliveries)
- `docs/NLR_MOU_v1.md` + `.docx` — current MOU governing this work
- `docs/NLR_PLAYBOOK.md` — JSC reply variants

---

## §14 — Contact + support

- **JSC technical lead (DC Hub):** Jonathan Martone — `jonathan@dchub.cloud`
- **Slack/Discord:** if your team prefers chat over email, we can stand up a dedicated channel
- **Pair programming sessions:** Ian + Jonathan can do live screen-shares; offer extended after the kickoff call
- **MCP server status:** `https://dchub.cloud/mcp` — server card at `/.well-known/mcp/server-card.json`
- **API health:** `https://dchub.cloud/healthz` returns 200 when production is healthy
- **OpenAPI spec:** `https://dchub.cloud/openapi.json`
- **Status page:** `https://dchub.cloud/status`
