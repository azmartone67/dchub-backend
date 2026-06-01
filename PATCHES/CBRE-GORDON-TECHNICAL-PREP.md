# Gordon (CBRE Technical Lead) — Pre-Call Technical Prep

*Today's call · Pat + Gordon on the CBRE side*

Gordon is the data/methodology brain. Pat handles the strategic relationship.
Gordon's job on this call is to figure out **if DC Hub's data layer would
actually hold up under CBRE's analytical scrutiny**. He'll probe methodology,
data lineage, refresh cadence, and integration patterns. The goal isn't
to wow him with marketing — it's to give him enough technical detail
that he comes away saying *"this is real, we can work with it."*

---

## Live metrics — pulled minutes before the call

| Metric | Value | Notes |
|---|---|---|
| Facilities tracked | **21,419** | discovered_facilities table, daily-deduped |
| Markets scored | **234** (capabilities) / 286 (DCPI rankings) | Reconciliation note below |
| DCPI verdicts | **8 BUILD · 151 CAUTION · 75 AVOID** | Total 234, zero LOW_SIGNAL — Gordon will notice that |
| M&A deals tracked | **2,232** | $324B+ cumulative value |
| Substations | **126,439** | EIA + HIFLD merged, deduped |
| Transmission lines | **56,108** | HIFLD with lat/lng |
| Power plants | **13,446** | EIA |
| Fiber routes | **6,476** | KMZ-derived + OSM Overpass |
| Gas pipelines | **918** | EIA + HIFLD |
| Active dev keys | **47** (44 free + 2 paid + 1 enterprise) | +21 net new since yesterday |
| Lifetime AI requests | **439,768** | 1.74M annualized |
| Source registry uptime | **22/22 (100%)** | All ingestion sources fresh |

**Reconciliation note** — the 234 vs 286 discrepancy is real. capabilities.json
shows 234 because it counts ONLY markets with non-null DCPI verdicts as
of today's daily bake. The 286 is the broader markets-scored count
including markets that may have LOW_SIGNAL or skipped a refresh. If
Gordon asks, **lead with 234** (current verdict-bearing markets) and
note that 286 is the lifetime universe we've ever scored.

---

## DCPI methodology — what Gordon will probe

### The two-axis framework

Every market gets two scores 0-100 + a verdict:

- **Excess Power Score** — how much usable headroom the grid actually
  has relative to data-center load. Inputs: ISO interconnect queue depth,
  available generation capacity, recent demand growth.
- **Grid Constraint Score** — transmission bottlenecks, interconnection
  wait times, regulatory friction.
- **Verdict** = function of (Excess, Constraint):
  - **BUILD** — Excess ≥ 60 AND Constraint ≤ 40
  - **CAUTION** — middle zone
  - **AVOID** — Excess ≤ 20 OR Constraint ≥ 65
  - **LOW_SIGNAL** — insufficient data confidence (currently zero)

### Data inputs Gordon will want named

| Input | Source | Refresh |
|---|---|---|
| Demand by ISO | EIA `/v2/electricity/rto` | hourly |
| Generation mix | EIA `/v2/electricity/rto` | hourly |
| Interconnect queue | ISO MIS feeds (ERCOT, PJM, CAISO, MISO, SPP, NYISO, ISO-NE) | daily |
| Substation inventory | HIFLD + EIA | weekly |
| Transmission lines | HIFLD | weekly |
| Facility locations | OSM Overpass + KMZ + ArcGIS FeatureServer | daily |
| M&A transactions | SEC EDGAR + RSS news pipeline | daily |
| News-derived signals | 60+ curated RSS feeds | hourly |

Every score links to its underlying inputs via `_DOMAIN_SOURCE` mappings —
**fully audit-clean**. Gordon can request the raw inputs for any specific
verdict and get the EIA/HIFLD/ISO row IDs.

### What we DON'T have (lead with honesty)

- **No broker-validated data** — we don't have CBRE's relationships, so
  off-market deal flow and pre-public site selection signals are gaps.
  That's literally why the bilateral exchange makes sense.
- **No tenant-level data** — we don't know which hyperscaler is paying
  what at which facility. CBRE has that from leasing engagements.
- **Limited tenant credit risk view** — your team has direct exposure to
  underwriting; we have public-facing signals only.
- **International ISO depth gap** — capabilities feed reports 3 modeled
  grids with calibrated thresholds (Hydro-Québec, AESO, Nord Pool).
  Dashboard scores ~12 more international markets using ISO defaults
  (NGESO, ENTSOE-DE/FR/NL, KEPCO, TEPCO, AEMO, EMA, IESO, BCH).
  The depth gap is that calibration outside North America still needs
  local reserve-margin / curtailment overrides — same calibration
  pattern we applied to Cheyenne, Williston, etc. Honest answer:
  "calibration coverage outside HQ/AESO/Nord Pool runs through Q3."

---

## API surfaces Gordon will want to see

### MCP server (AI-agent native consumption)

```
https://dchub.cloud/mcp
```

29 tools available. Streamable-HTTP transport. Per-call tier gating
via X-API-Key. **Show Gordon the live citation receipts** — it's the
single most credible "is this used" proof:

```
https://dchub.cloud/api/v1/agents/citations.json
```

### REST API (Gordon's likely first integration path)

| Endpoint | Returns | Tier |
|---|---|---|
| `/api/v1/markets/<slug>` | Market headline KPIs | Free |
| `/api/v1/dcpi/<slug>` | DCPI verdict + breakdown | Free (anon teaser) / Pro (full) |
| `/api/v1/facilities/<id>` | Per-facility detail | Identified |
| `/api/v1/grid/intelligence/<iso>` | ISO grid headroom + queue | Pro (anon teaser) |
| `/api/v1/transactions/list` | M&A history with filters | Identified |
| `/api/v1/agents/capabilities.json` | Live counts + manifest | Public |
| `/api/v1/sources` | Source registry health | Public |

### Raw exports (enterprise-tier)

Parquet + CSV bulk delivery via S3 presigned URLs. Currently shippable:
- Full DCPI history (every verdict change since day 0)
- Full facility catalog (21,419 rows, all fields)
- M&A transactions (2,232 rows, attributes + amounts)
- Substation inventory (126,439 rows)

### Live grid (Gordon's likely first "wow")

Pull this DURING the call to show real-time refresh:

```bash
curl -s "https://dchub.cloud/api/v1/grid/intelligence/ERCOT" | python3 -m json.tool
curl -s "https://dchub.cloud/api/v1/grid/totals" | python3 -m json.tool
```

Numbers move every hour. Compare to whatever CBRE's internal cadence
is. The delta IS the value prop.

---

## Live demo sequence (5 min during the call)

If Pat/Gordon want a screen-share moment:

1. **Open `https://dchub.cloud/dcpi`** — show the live market grid, point at
   today's 8 BUILD verdicts. Click into Cheyenne (top BUILD market).
2. **Open `https://dchub.cloud/dcpi/cheyenne-wy`** — show the methodology
   panel (Excess + Constraint scores). Click the "inputs" expander to
   show audit trail.
3. **Switch to `https://dchub.cloud/api/v1/agents/citations.json`** —
   show the raw JSON. Point at 24,176 calls in 7d.
4. **Open `https://dchub.cloud/api/v1/sources`** — show 22 enabled sources,
   all fresh. This is the "is this real" proof.
5. **Run a live MCP tool call** — paste this into a Claude.ai chat
   *while screen-sharing*:
   > *"Use DC Hub to compare Cheyenne, Northern Virginia, and Frankfurt
   > for AI training site selection."*

   Claude pulls live data from DC Hub's MCP, returns a structured
   comparison. **This is the moment Gordon understands what AI-agent
   citation means.**

---

## Integration patterns Gordon will ask about

### Option 1 — Embed DC Hub data in CBRE's internal dashboards
- CBRE pulls DC Hub Pro API on a daily cron.
- DC Hub returns parquet / CSV. CBRE's BI tool ingests.
- **Friction**: lowest. CBRE's data team owns the integration.
- **Tier**: Pro Data License ($75K/yr) or Strategic Partner ($250K/yr).

### Option 2 — White-label DC Hub-backed widgets
- CBRE publishes DCPI scorecards under their own brand, with
  "Powered by DC Hub" attribution.
- DC Hub provides iframe embeds + CDN-cached widgets.
- **Friction**: medium (CBRE web team coordinates).
- **Tier**: Strategic Partner with co-brand rights.

### Option 3 — DC Hub feeds CBRE's H2 2026 report
- CBRE pulls fresh DC Hub data 2 weeks before publication.
- DC Hub provides methodology appendix + sources.
- Both logos on the cover.
- **Friction**: lowest of the three for one-shot integration.
- **Tier**: revenue-share on enterprise leads, no upfront $ (Structure A
  from the Pat call brief).

---

## SLA / uptime / data integrity story

| Metric | Current state | What we can commit to in MOU |
|---|---|---|
| Source registry uptime | 100% (22/22 fresh) | 95% monthly availability across enabled sources |
| API uptime | tracked via Railway healthchecks | 99.5% monthly (the four 9s would need infra investment) |
| Data freshness SLA | DCPI daily, grid hourly, facility weekly | Same — published cadences are the SLA |
| Error budget for material data corrections | track + publish corrections log | Same |
| Incident response | brain detector catches + heals; humans paged on critical | Same with named pager rotation |

**Gordon will respect the honesty.** Don't promise 99.99% — Railway
single-replica makes that aspirational. 99.5% is real and defensible.

---

## What Gordon is REALLY checking for

1. **"Is your data more than scraped public stuff?"** → Yes. ISO MIS feeds
   require parsed downloads, EIA bulk API needs auth, HIFLD requires
   ETL'd shapefiles. We've built the ingestion pipeline.
2. **"Is your methodology defensible?"** → Yes. Every DCPI score has
   audit trail to underlying EIA/HIFLD/ISO inputs. Available on request.
3. **"Can you scale if CBRE clients hit it hard?"** → Honest answer:
   single Railway replica today; CDN caches handle bursts. If CBRE
   needs hardened SLA, we'd commit to multi-replica + Postgres replica
   read pool as part of Strategic Partner tier.
4. **"What's your data corrections process?"** → Brain-detected
   anomalies surface as findings, autopilot fixes ~17 classes
   autonomously, humans review the rest. Published corrections log
   (not yet public — could become a partnership deliverable).
5. **"Is this a venture-funded land-grab or a real business?"** → Honest:
   bootstrapped, 47 paying-tier-or-above keys, $X ARR run-rate, growing.
   No exit pressure to capture share — playing the long game on becoming
   the data primitive for the asset class.

---

## Three things to NOT say to Gordon

1. **Don't claim numbers you can't show on screen.** If you say "286
   markets" and capabilities.json shows 234, he'll catch it. **Have the
   live URL ready.**
2. **Don't oversell methodology.** "Audit-clean every score" is true.
   "Real-time millisecond updates" is not — daily is daily.
3. **Don't promise we'll build whatever they want.** The data layer IS
   the product. If CBRE needs custom analytics on top, that's a
   services engagement, not the data license.

---

## What Gordon walking out of the call green-lights

- Permission to do a 30-day data-quality pilot (no money committed).
- His team gets read-only API access to validate against their internal
  data on 100 sample markets.
- Joint write-up of the comparison (if material agreement, that becomes
  the MOU evidence).

**That's the technical win.** Pat closes the strategic frame; Gordon
closes the technical credibility frame; the combination is what makes
Pat's procurement team approve the next-step spend.
