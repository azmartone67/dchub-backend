# Data provenance register

**Status: draft for legal review. Not legal advice.**

Every external source DC Hub ingests, what it feeds, and whether the terms it
arrives under permit us to redistribute it — including under a partner's brand.

This exists because "we're open source and it's all public data" is three
separate questions, not one:

1. **The code** — what licence do we publish it under?
2. **The database** — what licence did each source arrive under, and what does
   that oblige us to do when we redistribute?
3. **The claims** — who is liable for what the product asserts?

Open source answers only (1). A white-label deal turns on (2) and (3).

## How this was built

Three passes over the repo:

1. Extracted every external host referenced in non-vendored `.py` (529 hosts,
   most of them email, payments, or URLs sitting inside prompt strings).
2. Enumerated the 63 files matching
   `ingest|loader|import|backfill|crawler|scraper|_sync|fetch_|load_|seed_`
   under the repo root and `routes/`.
3. Read the source and licence markers out of the 35 of those that fetch
   external data. The remainder are internal (SQLite→Neon migration, D1 and
   vector sync, column backfills) and ingest nothing from outside.

Sources with no external host — manual or admin-ingested — are in §5.2.

**Two columns are different in kind and must not be conflated.** *Source* and
*ingested by* are **facts** read out of the code. *Licence* and *redistributable*
are **assessments** that a lawyer must confirm against each source's current
terms — several are marked `VERIFY` precisely because I could not confirm them
from the repo. Nothing here is a legal conclusion.

---

## 1. US federal / public domain — low risk

Work product of the US government, generally not subject to copyright.

| Source | Feeds | Ingested by | Licence | Redistributable |
|---|---|---|---|---|
| EIA (`api.eia.gov`, `www.eia.gov`) | 13k US power plants, gas prices, EIA-860M planned generators | `eia860_bulk_loader.py`, `eia_gas_prices_loader.py`, `routes/planned_generators_ingest.py` | US Gov — public domain | Yes |
| EIA ArcGIS (`services2.arcgis.com`) | ~30k gas pipeline segments, transmission lines | `routes/gas_pipeline_ingest.py`, `routes/transmission_ingest.py`, `routes/power_plants_ingest.py` | US Gov — public domain | Yes |
| HIFLD (`hifld-geoplatform.opendata.arcgis.com`, `services1/5.arcgis.com`) | 126k substations, 94k transmission lines | `hifld_substation_loader.py`, `load_hifld_transmission.py`, `load_substations.py`, `routes/substation_ingest.py` | HIFLD Open — public | Yes — **VERIFY**: some HIFLD layers moved to restricted access; confirm ours are still in HIFLD *Open* |
| FCC Broadband Data (`broadbandmap.fcc.gov`) | Carrier-to-facility mapping | `carrier_facility_ingestion.py` | US Gov — public domain | Yes |
| LBNL "Queued Up" | `lbnl_queue` interconnection research set | `load_lbnl_queue.py` | DOE lab — **VERIFY** (usually CC BY) | Likely, with attribution |

## 2. Open licence with obligations — attribution and/or share-alike

Free to use. **Not** free of conditions. This is the tier most often assumed to
be "public data, no strings."

| Source | Feeds | Ingested by | Licence | Obligation |
|---|---|---|---|---|
| **OpenStreetMap** via Overpass (`overpass-api.de`, `overpass.kumi.systems`, `overpass.openstreetmap.ru`) | Substations, power plants, transmission, pipelines, comms towers; **international DC facility records**; EMEA/APAC infrastructure | `osm_overpass_loader.py`, `intl_infra_ingest.py`, `fetch_emea_apac_infrastructure.py`, `routes/osm_crawler.py`, `facility_ingestion.py` | **ODbL 1.0** | **Attribution + share-alike.** See §5.1 |
| Global Energy Monitor | 182k global generating units, LNG terminals | `routes/gem_ingest.py` | CC BY 4.0 — **VERIFY** | Attribution |
| WRI Aqueduct | Water-stress scores | `routes/water_aqueduct_ingest.py` | CC BY 4.0 — **VERIFY** | Attribution |
| NSW Government data portal | Feeder hosting capacity (AU) | `routes/hosting_capacity_ingest.py` | Usually CC BY — **VERIFY** | Attribution |

## 3. Third-party terms of use — verify individually

Public to *view*. Redistribution and commercial resale are governed by each
operator's terms, which differ and several of which restrict exactly this.

| Source | Feeds | Ingested by | Redistribution |
|---|---|---|---|
| ISO/RTO portals — PJM, ERCOT, CAISO, MISO, NYISO, SPP, ISO-NE | Interconnection queues; real-time LMP | `load_interconnect_queue_live.py`, `routes/iso_queue_ingest.py`, `routes/iso_lmp_ingest.py` | **VERIFY each of 7.** Several ISO ToUs restrict commercial redistribution |
| ENTSO-E Transparency | EU grid | `routes/international_ingestion.py` | **VERIFY** — registration-gated, terms attach |
| NESO / National Grid ESO, AESO, EMA Singapore, JEPX | International DCPI inputs | `routes/international_ingestion.py` | **VERIFY** per jurisdiction |
| Utility ArcGIS — SCE, NV Energy, Exelon | Feeder hosting capacity | `routes/hosting_capacity_ingest.py` | **VERIFY** per utility |
| PeeringDB | Networks, IX, campus, carrier-facility | `network_ix_ingestion.py`, `carrier_facility_ingestion.py` | **VERIFY** — believed CC BY 4.0; attribution required either way |
| `buildingpermit.io`, county permit portals | Permit enrichment | `permit_scraper.py` | **VERIFY** — commercial API terms + municipal portal terms |

## 4. Commercial / proprietary — resolve before any white-label

| Source | Feeds | Ingested by | Issue |
|---|---|---|---|
| **TeleGeography** (`submarinecablemap.com`, GitHub mirror) | 690 subsea cables, 1,900 landing points | `subsea_cable_ingestion.py` | Proprietary commercial dataset. Free to view; commercial redistribution restricted. TeleGeography sells this. **Blocker** |
| **Cloudscene** | Facility directory rows | `facility_ingestion.py` → `fetch_cloudscene()` | Scrapes a commercial competitor's listings. No `robots.txt` check in the code path; UA is `Mozilla/5.0 (compatible; DC-Hub-Bot/1.0)`. **Blocker** |
| DataCenterMap | Facility seed rows (`source='datacentermap'`) | `routes/datacentermap_crawler.py` | Crawler conduct is good — ToS risk was weighed on the record, `robots.txt` parsed, self-identifying UA + contact, 2s rate limit, 250-row cap, off by default. Then abandoned when DCM proved hostile to crawlers (see `routes/osm_crawler.py`). **Rows from earlier runs may persist**; purge path exists |
| Trade press — DataCenterDynamics, DataCenterFrontier, DataCenterKnowledge, Capacity Media, Bloomberg feeds, Google News | 1,700 tracked M&A deals; news surface | `deal_scraper.py` | Copyright in headlines, snippets and article text; Google News and Bloomberg feed ToS. Facts about a deal aren't protectable — the expression is. **Assess how much text is retained and served** |

## 5. Obligations we do not currently meet

### 5.1 ODbL attribution and share-alike

`routes/osm_crawler.py` states OSM is "100% open data (ODbL license)" — so the
licence is known. But:

- **No attribution surface exists.** `static/ai-data-source.html` names no
  source at all.
- ODbL is **share-alike**: publicly distributing a *derived database* obliges
  us to offer that derived database under ODbL and to attribute OSM.
- `export_dataset` → `/api/v1/facilities/export` distributes in bulk. That is
  the share-alike trigger, not mere display.

This affects a large share of the mapped-asset layers and some facility rows,
and it flows to any white-label partner as a distributor.

### 5.2 Tenant provenance is not recorded per row

`routes/tenant_directory.py` exposes `POST /api/v1/tenants/ingest` for admin
bulk ingest. The row carries a free-text `source` and `source_url`, and the
uniqueness index uses `COALESCE(source, '')` — so **`source` is nullable and
some rows may have none**.

Facility tenancy is frequently confidential under lease and NDA. A partner
already operating in this industry may hold NDAs with the very operators named.
Until every row can answer "where did this come from," this is unresolvable by
document review alone — it needs a data audit.

### 5.3 No LICENSE file

The repo is public with no `LICENSE`, which means all rights reserved by
default. Nothing in the repo grants a partner anything; every right must come
from the contract.

## 6. What DC Hub owns

Derived work — our own IP, but it inherits the obligations of its inputs:

- DCPI market scores, composite site scores, rankings
- `constraint_coverage` — the block naming what a query *cannot* answer
- The `replay` audit trail — per-step rationale and constraint checks
- Tool surface, MCP layer, all application code

`constraint_coverage` is worth putting in front of counsel deliberately. A tool
that publishes its own limits is a materially better position on reliance than
one that implies completeness.

## 7. Recommended order of work

1. **TeleGeography** — licence it, or drop/replace the subsea layer. Highest
   chance of a letter.
2. **Cloudscene** — remove `fetch_cloudscene()`, or replace with a licensed
   source. Purge derived rows.
3. **Tenant provenance audit** — make `source` NOT NULL; backfill or purge rows
   that cannot answer where they came from.
4. **ODbL compliance** — attribution page, licence notice on OSM-derived
   layers, and a decision on the share-alike obligation `export_dataset`
   triggers.
5. **`LICENSE` file** — code licence, and a separate, explicit data licence.
6. **DataCenterMap rows** — confirm whether any persist; purge if so.
7. **ISO/RTO terms** — read all seven; some may bar commercial redistribution.
8. **Deal/news retention** — measure how much source text is stored and served.
9. **White-label agreement** — carve-out from our own ToS (which today forbids
   sublicensing and redistribution outright), mutual indemnities, and a DPA
   covering the visitor and API-key tracking.

## 8. What this document cannot tell you

- **Whether production rows match this code.** This is read from ingestion
  source. Rows loaded by one-shot scripts, manual imports, or paths since
  deleted will not appear. Only a `SELECT DISTINCT source` against production
  closes that gap.
- **Current terms.** Every `VERIFY` needs reading against the source's terms as
  they stand today, not as they were when the ingest was written.
- **Whether any of this is actually infringing.** That is a lawyer's call. This
  is a map of where to look.
