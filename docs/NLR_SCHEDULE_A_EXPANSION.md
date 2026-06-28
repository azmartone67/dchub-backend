# DC Hub × NLR — Schedule A Expansion Proposal

**Version 1 — 2026-06-09 — DRAFT for JSC review**

In the 14 days between key issuance (2026-05-26) and JSC kickoff (2026-06-10), DC Hub shipped **~90 new public endpoints** addressing data-center siting, infrastructure intelligence, methodology, and reporting. This document proposes amendments to the MOU's Schedule A endpoint surface to reflect what's now available — both for NLR's immediate use under the existing Tier 0 license and as the basis for the contracted expansion.

Recommended MOU amendment mechanism: written addendum signed by both JSC executive sponsors, no full re-execution required.

---

## TL;DR — Recommended new schedules

| New schedule | What it adds | reVeal gap addressed |
|---|---|---|
| **A.6 Global Infrastructure** | 6 endpoints — global gas, hazards, IXPs, power plants, submarine cables, cable landing points | Extends reVeal beyond US-only; supports international DC siting research |
| **A.7 Site Briefs and Reports** | 7 endpoints — site report, site value, state-brief, market-brief, operator-brief, hyperscaler-brief, site-selection canvas | Pre-computed analytical artifacts — saves NLR researcher time |
| **A.8 Methodology and Data Dictionary** | 2 endpoints — methodology + data-dictionary.json | **Satisfies MOU Schedule E.1** (Data Dictionary 60-day deliverable) — already shipped |
| **A.9 Reports and Exports** | 5 endpoints — quarterly reports, pipeline, state-of-power, bulk exports | Formalizes MOU Schedule B.5 quarterly-snapshot delivery via live API |
| **A.10 Deal Intelligence** | 4 endpoints — deal-autopsy, DCGI scores, DCPI snapshot/total | Validation against operational signal (M&A as real-world signal) |
| **A.1 (existing) — additions** | 3 endpoints — grid-transition/radar, ercot/realtime, grid/intelligence/<region> | **Direct ERCOT realtime for Texas Triangle pilot** |

Total new endpoints: **~27 high-value additions** to Schedule A, all live in production today.

---

## A.6 — Global Infrastructure (new schedule)

**Why this matters to NLR:** reVeal's current scope per the March 2026 deck (NLR/PR-6A20-99256) is US-only. As soon as NLR considers international DC siting research, these endpoints provide the foundation. Even for US-focused work, the submarine-cable + cable-landing-point data is critical for understanding hyperscale network architecture.

| Endpoint | Returns | Status |
|---|---|---|
| `/api/v1/infrastructure/global-gas` | Global natural gas infrastructure (pipelines, compressors, storage) | ✅ Live |
| `/api/v1/infrastructure/global-hazards` | Global hazard overlays (seismic, flood, hurricane) — extends US FEMA | ✅ Live |
| `/api/v1/infrastructure/global-ixps` | 1,300+ global IXPs via PeeringDB — the connectivity moat | ✅ Live |
| `/api/v1/infrastructure/global-power-plants` | Global power plants (OSM Overpass) | ✅ Live |
| `/api/v1/infrastructure/submarine-cables` | Submarine cable network — critical for hyperscale fiber routes | ✅ Live |
| `/api/v1/cable-landing-points` | Submarine cable landing facilities | ✅ Live |

---

## A.7 — Site Briefs and Reports (new schedule)

**Why this matters to NLR:** these are pre-computed analytical reports — NLR researchers don't have to assemble them. For the Validation Study, comparing reVeal output against the `site-report` or `site/value` composite gives a direct apples-to-apples reference.

| Endpoint | Returns | Status |
|---|---|---|
| `/api/v1/site-report` | Full site-level report (composite + per-layer scoring) | ✅ Live |
| `/api/v1/site-report/portal` | HTML-rendered portal version | ✅ Live |
| `/api/v1/site/value` | Site-valuation composite | ✅ Live |
| `/api/v1/site/value/methodology` | Valuation methodology documentation | ✅ Live |
| `/api/v1/site-selection/canvas` | Site-selection canvas (multi-criteria evaluation) | ✅ Live |
| `/api/v1/state-brief/<state>` | State-level data-center brief | ✅ Live |
| `/api/v1/market-brief/<slug>` + `/all` + `/all.csv` + `/diff` | Per-market briefs across 300+ markets | ✅ Live |
| `/api/v1/operator-brief/<slug>` | Per-operator profile and footprint | ✅ Live |
| `/api/v1/hyperscaler-brief/<slug>` | Per-hyperscaler footprint and pipeline | ✅ Live |
| `/api/v1/operators/profiles` + `/operators/<canonical>/profile` | Operator profile data | ✅ Live |

---

## A.8 — Methodology and Data Dictionary (new schedule)

**Critical: this satisfies MOU Schedule E.1's 60-day Data Dictionary deliverable on Day 1.** The endpoint is live, machine-readable, and covers all Schedule A endpoints (and the proposed A.6-A.10 additions).

| Endpoint | Returns | Status |
|---|---|---|
| `/api/v1/methodology` | Human-readable methodology page | ✅ Live |
| `/api/v1/methodology/data-dictionary.json` | Machine-readable JSON Schema for all licensed endpoints — covers schema, upstream source, refresh cadence, known limitations per Schedule E.1 | ✅ Live |

**Recommendation:** Add to MOU Article VII as the canonical Data Dictionary source. Reduces a future deliverable obligation to a today-already-satisfied check-the-box.

---

## A.9 — Reports and Exports (new schedule)

**Why this matters to NLR:** these formalize MOU Schedule B.5's quarterly-snapshot delivery commitment via a structured API surface. Instead of out-of-band Parquet drops, NLR can pull a snapshot on demand. Both delivery mechanisms remain available (NLR's choice).

| Endpoint | Returns | Status |
|---|---|---|
| `/api/v1/reports/pipeline` | Construction pipeline report | ✅ Live |
| `/api/v1/reports/quarterly/<quarter>.csv` + `.json` | Quarterly snapshots in NLR's choice of format | ✅ Live |
| `/api/v1/reports/state-of-power` | State-of-power report | ✅ Live |
| `/api/v1/exports/build` + `/exports/<name>` | Custom exports on demand | ✅ Live |

---

## A.10 — Deal Intelligence (new schedule)

**Why this matters to NLR:** the reVeal Validation Study compares reVeal projections against ground-truth signals. M&A deals are a strong ground-truth signal for operational data-center activity — they represent realized commercial bets. The deal-autopsy endpoint provides forensic-level data per deal.

| Endpoint | Returns | Status |
|---|---|---|
| `/api/v1/deal-autopsy` | Per-deal forensic analysis | ✅ Live |
| `/api/v1/dcgi/scores` + `/<state>` | Data Center Geographic Intelligence scores | ✅ Live |
| `/api/v1/dcpi/snapshot` + `/dcpi/total` | DCPI (Data Center Power Index) — 300+ markets | ✅ Live |
| `/api/v1/dcgi/methodology` + `/operators` | DCGI methodology + per-operator breakdown | ✅ Live |

---

## A.1 (existing) — Additions

**Why this matters to NLR:** the existing A.1 Grid section becomes substantially more powerful with these three additions. **ERCOT realtime is critical** because the Texas Triangle was one of the two proposed pilot regions in the Product Roadmap (the other was PJM/Ashburn).

| Endpoint | Returns | Status |
|---|---|---|
| `/api/v1/grid-transition/radar` | Grid transition radar — early warning of capacity changes | ✅ Live |
| `/api/v1/ercot/realtime` | **ERCOT real-time** — frequency, load, generation by fuel type | ✅ Live |
| `/api/v1/grid/intelligence/<region>` | Region-specific grid intelligence (per-region detail vs national rollup) | ✅ Live |

---

## What's intentionally NOT proposed for inclusion

These endpoints exist but are excluded from the proposed expansion either because they're not relevant to research use or because they're commercial-tier features:

- `/api/v1/admin/*` — DC Hub administrative (not licensable to partners)
- `/api/v1/brain/*` — Internal AI orchestration
- `/api/v1/agent/cookbook`, `/agent/recipe/*`, `/agent/solve` — Agent concierge for paying customers
- `/api/v1/competitive/*` — Competitive intelligence (commercial)
- `/api/v1/pricing/ab-*` — A/B testing infrastructure (internal)
- `/api/v1/strategic-scaffold/*` — Sales / internal
- `/api/v1/team/*` — Team management (multi-user, available to NLR if needed but separate scope)
- `/api/v1/connect/*`, `/api/v1/keys/auto-trial/bind` — Auth / onboarding plumbing
- `/api/v1/og/dynamic.png`, `/api/v1/widget-embeds/*` — Marketing surface
- `/api/v1/url-registry/*`, `/api/v1/slo/*`, `/api/v1/vertex/health` — Internal ops

If NLR wants any of these for a specific use case, JSC discussion can grant case-by-case access without a formal Schedule A amendment.

---

## Proposed amendment mechanism

Per MOU Article XIV (Change Management), Schedule A modifications require written addendum signed by both JSC executive sponsors. Recommended sequence:

1. **JSC Q1 review (90 days post-execution)** — formally adopt this proposal as Schedule A Addendum 1
2. **Or sooner** — JSC ad-hoc consensus at the kickoff call can pre-approve the addendum subject to NLR Legal review
3. **Or immediately** — under the existing MOU's "JSC consensus" mechanism (Article V § 5.3), all listed endpoints can be enabled for NLR Developer keys today; the formal Schedule A textual addendum follows at convenience

DC Hub's preference: **option 3** — enable today, formalize when convenient. NLR's keys already authenticate against the full surface area; this addendum just codifies it in writing.

---

## Specific value for tomorrow's call

**Bring this up around the methodology questions:**

> *"In the 14 days since you got keys, we shipped roughly 27 new endpoints that map directly to reVeal's stated needs — including the ERCOT realtime feed you'd want for the Texas Triangle pilot, global IXP data for international expansion, and a machine-readable Data Dictionary that satisfies MOU Schedule E.1 ahead of the 60-day deadline. The full proposed Schedule A expansion is in `docs/NLR_SCHEDULE_A_EXPANSION.md`. Want me to walk through the categories most relevant to your validation work?"*

That's a forward-pull anchor. It signals:
- DC Hub is shipping fast (good supplier signal)
- The additions are NLR-targeted (the ERCOT realtime is named, not coincidental)
- We're already ahead on Schedule E.1 deliverables (good faith on contract obligations)
- This is the JSC's call (deferential — they decide what's in scope)

---

## File pointers

- `docs/NLR_MOU_v1.md` — the current MOU (Schedule A is in Articles VII + the Schedules section)
- `docs/NLR_PRODUCT_ROADMAP.md` — broader product timeline (this expansion fits as a Day-1 acceleration)
- `docs/NLR_PARTNERSHIP_ROADMAP.md` — historical background
