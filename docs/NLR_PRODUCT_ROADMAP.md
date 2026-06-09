# DC Hub × NLR — Product Roadmap

**Version 1 — 2026-06-09 — DRAFT for JSC review**

Maps the engagement from Day-1 access through the 24-month Initial Term. Built to be screen-share-friendly on the JSC kickoff call and shareable with Gabe / Galen / Ian directly.

This is the **product** roadmap (what DC Hub builds + delivers to NLR). For the **commercial** roadmap (fees, tier transitions, contract milestones) see `NLR_MOU_v1.md`. For the **negotiation** roadmap (counsel cycle, response variants) see `NLR_PLAYBOOK.md`.

---

## TL;DR

| Phase | When | What lands |
|---|---|---|
| **Day 1** | Now (keys live) | Full Schedule A — ~25 endpoints, 6 reVeal-specific, OpenAPI + MCP server |
| **30 days post-execution** | MOU+30d | Data Dictionary (Schedule E), per-endpoint methodology docs |
| **60 days** | MOU+60d | All server-side path aliases, first quarterly `discovered_facilities` snapshot |
| **90 days** | MOU+90d | Validation-rig prototype, Stream C scaffold (private), JSC Q1 review |
| **180 days** | MOU+180d | Validation Study draft, Stream C `dchub-revealkit` public launch |
| **12 months** | Year 1 close | Validation Study submitted, reVeal v2 integration prep, Tier 0 → Tier 1 |
| **24 months** | End of Initial Term | Validation Study published, reVeal v2 integrated, Tier 2 evaluation |

---

## Day 1 — What NLR has today

**Effective immediately, pre-execution Developer keys are live for Gabriel, Galen, and Ian.** Full Schedule A surface, no Pro-tier paywall gates, no rate-limit ceiling within reason.

### Endpoint surface — already live, exercised today

**A.1 Grid and Interconnection (6)**
- `/api/v1/grid-headroom` — per-region reserve margin
- `/api/v1/grid-intelligence` — composite grid signal (reserve + queue depth)
- `/api/v1/grid-data` *(alias: `/grid/data`)* — raw ISO load timeseries
- `/api/v1/interconnection-queue` — ISO queue snapshots
- `/api/v1/infrastructure` — HIFLD substations (79K+) + FEMA hazard overlay
- `/api/v1/energy-prices` *(alias: `/energy/retail`)* — EIA state-level retail rates

**A.2 Siting Variables (6)**
- `/api/v1/air-permitting` — state-level air-permitting posture
- `/api/v1/tax-incentives` — 50-state DC tax abatements
- `/api/v1/water-risk` *(alias: `/water/stress`)* — USGS water-stress
- `/api/v1/fiber-intel` *(alias: `/fiber/intel`)* — per-facility carrier intel
- `/api/v1/renewable-energy` *(alias: `/energy/renewable`)* — renewable + PPA depth
- `/api/v1/geothermal-potential` — geothermal score

**A.3 Composite Intelligence — reVeal-aligned (7)**
- `/api/v1/reveal-cell` — cell-level composite for reVeal input
- `/api/v1/colocation-score` — colocation viability score
- `/api/v1/microgrid-viability` — microgrid feasibility
- `/api/v1/intelligence-index` — composite siting index
- `/api/v1/analyze-site` — single-site full report
- `/api/v1/compare-sites` — multi-site comparison
- `/api/v1/dchub-recommendation` — DC Hub composite recommendation

**A.4 Market and Facility Data (5 + 1 snapshot)**
- `/api/v1/facility`, `/search-facilities`, `/pipeline`, `/market-intel`, `/news`, `/list-transactions`

**A.5 reVeal-specific (6 — all live, no longer "in development")**
- `/api/v1/reveal-cell-bulk` — bulk cell composite by bounding box
- `/api/v1/reveal-grid-export` + `/status/<job_id>` — async grid export
- `/api/v1/reveal-validation-feed` — validation feed
- `/api/v1/social-acceptance-index` — local-opposition signal (fills slide-25 gap)
- `/api/v1/climate-risk` — climate-risk overlay
- `/api/v1/carbon-intensity` — carbon-intensity timeseries

### Documentation already available
- **OpenAPI spec:** `https://dchub.cloud/openapi.json`
- **MCP server:** `https://dchub.cloud/mcp` (server card at `/.well-known/mcp/server-card.json`)
- **Live partner landing page:** `https://dchub.cloud/partners/nlr` *(sanitized stub pending JSC sign-off on public copy)*

### What this directly addresses (per the March 2026 reVeal deck)

NLR's own paper (`NLR/PR-6A20-99256`, Zuckerman/Igwe/Williams) flags four spatially-explicit data gaps that cause reVeal predictions to cluster around population centers:

| reVeal's flagged gap | DC Hub endpoint that fills it | Status |
|---|---|---|
| **Local transmission hosting capacity** | `/grid-intelligence` + `/interconnection-queue` | ✅ Live Day 1 |
| **Load interconnection queue times** | `/interconnection-queue/snapshot` | ✅ Live Day 1 |
| **Zoning / permitting** | `/tax-incentives` (partial), `/air-permitting` (state-level) | 🟡 Partial — see 12-month plan |
| **Social acceptance** | `/social-acceptance-index` | ✅ Live Day 1 |

**Three of four gaps are addressable today.** The fourth (parcel-level zoning) is a 12-month plan item.

---

## 30 Days Post-Execution

**Contractual deliverables under Schedule E.1.** Owners listed.

| Item | Owner | Definition of done |
|---|---|---|
| **Data Dictionary v1** | DC Hub | Per-endpoint JSON Schema + Markdown index at `dchub.cloud/datadictionary`. Documents schema, upstream source (FERC/ISO/EIA/EPA/USGS/HIFLD), refresh cadence, known limitations for each Schedule A endpoint. |
| **Authentication runbook** | DC Hub | One-pager covering key rotation, expiration, revoke procedure, error-code reference. |
| **NLR security contact onboarded** | NLR | Schedule F.5 — NLR provides incident-notification contact. |
| **NLR procurement + billing contacts** | NLR | Schedule B.4 — for Year-2 Tier 1 invoicing. |
| **First "office hours" call** | Both | 30-minute screen-share session, Ian + Galen + Jonathan. Walk through OpenAPI, answer integration questions. |

---

## 60 Days Post-Execution

**Per Article XIV § 14.4 (alias maintenance) and Schedule B.5 (snapshot delivery).**

| Item | Owner | DoD |
|---|---|---|
| **All server-side path aliases shipped** | DC Hub | All 5 alias pairs from Schedule G.4 resolve to the same content. License-cited paths resolve. |
| **First quarterly `discovered_facilities` snapshot** | DC Hub | Parquet + GeoJSON dump delivered to NLR via S3 or DOE-acceptable transfer. Includes full schema doc + diff-from-prior section. |
| **Initial reVeal Characterize integration** | NLR (Ian) + DC Hub support | Ian's first PR against `NatLabRockies/reveal` (or his integration repo) that consumes a DC Hub endpoint live. Ideally `/site-forecast` or `/water/stress`. |
| **MCP server connection (optional)** | NLR | Galen or Ian configures Claude.ai / Cursor / ChatGPT custom connector to query DC Hub via MCP. Useful for ad-hoc methodology exploration. |

---

## 90 Days Post-Execution (JSC Q1 Strategic Review)

**Validation rig prototype + Stream C scaffold + Q1 health check.**

| Item | Owner | DoD |
|---|---|---|
| **Validation rig prototype** | NLR (Galen) + DC Hub | Reproducible script that runs reVeal for a pilot region (PJM or ERCOT corridor confirmed by JSC), pulls DC Hub `/site-forecast` for the same cells, computes residuals. Internal-only deliverable. |
| **First validation findings memo** | Galen + Jonathan | 5-10 page internal memo. Pre-paper. Includes: pilot scope, methodology summary, top-10 residual cells, hypotheses for the difference, plan to extend to full study. Pre-submission honesty-clause review (Article IX) doesn't apply yet — internal only. |
| **Stream C `dchub-revealkit` scaffold** | DC Hub | Private DC Hub-org repo. README, LICENSE, `pyproject.toml`, basic `client.py`, one worked example. NOT public yet. |
| **JSC Q1 Strategic Review** | All | 60-90 minute video. Topics: validation progress, Schedule A endpoint additions/changes NLR wants, public-launch readiness for Stream C, MOU Tier 0 → Tier 1 transition prep. |

---

## 180 Days Post-Execution

**Validation Study draft + Stream C public launch.**

| Item | Owner | DoD |
|---|---|---|
| **Validation Study draft v1** | All co-authors (Galen lead) | Full peer-review-grade manuscript. Pilot region's results, methodology, statistical analysis of residuals, discussion. Circulated to all JSC members + co-authors for 30-day pre-submission review per Article IX § 9.3. |
| **Stream C `dchub-revealkit` public launch** | DC Hub + NLR (Galen review) | Public GitHub repo. Apache 2.0. README clearly distinguishing from upstream reVeal. Tagged v0.1.0. Press-release-eligible (subject to NLR JSC approval per Article XII § 12.4). |
| **Pre-submission honesty-clause review** | Both Parties | Article IX § 9.3 — 30 days minimum before journal submission. Each Party reviews for (i) factual accuracy of its own contribution, (ii) confidentiality, (iii) attribution. Honesty Clause prevents either Party from blocking findings unfavorable to its interests. |
| **JSC Q3 strategic review** | All | Topics: paper readiness, journal selection, Stream C adoption signals, Year-2 Tier 1 contract preparation. |

---

## 12 Months Post-Execution (Year 1 close)

**Paper submission + Tier 0 → Tier 1 transition + reVeal v2 prep.**

| Item | Owner | DoD |
|---|---|---|
| **Validation Study submitted to journal** | All co-authors | Manuscript submitted to JSC-selected peer-reviewed venue. Activates Article XIV § 14.3 — cited-endpoint stability commitment locks in for 12 months from submission date. |
| **Tier 0 → Tier 1 transition** | NLR | Per Article VI § 6.3 — automatic transition to $10K/yr Tier 1 rate. Renewal-fee-adjustment mechanism (CPI-U capped at 5%) activates. |
| **Parcel-level zoning endpoint (`/api/v1/zoning`)** | DC Hub | New endpoint addressing reVeal's #3 flagged gap. Parcel-level zoning code + permitting friction score. Initial coverage: top 20 US data-center markets. **Schedule A amendment required** (mutual JSC sign-off). |
| **reVeal v2 first-look preparation** | NLR + DC Hub | If NLR ships reVeal v2 in this window, DC Hub gets first-look access per Schedule B.5 in-kind exchange. DC Hub builds an integration-readiness checklist and identifies which existing endpoints need schema updates. |

---

## 24 Months Post-Execution (End of Initial Term)

**Paper published + Stream C in academic adoption + Tier 2 evaluation.**

| Item | Owner | DoD |
|---|---|---|
| **Validation Study published** | All co-authors | Peer-reviewed publication appears in journal. Triggers institutional press from both sides (NLR press release + DC Hub press release, both Article XII-approved). |
| **Stream C v1.0 release** | DC Hub | `dchub-revealkit` reaches v1.0 — API stable, used in academic publications outside the NLR/DC Hub team, ≥10 external citing publications. |
| **reVeal v2 fully integrated** | DC Hub | If reVeal v2 has shipped, DC Hub provides updated endpoint schemas for any reVeal v2-specific input format. |
| **Tier 2 evaluation** | NLR | If NLR's dedicated DC-siting research funding has closed in the interim, Tier 2 ($25K/yr) is considered. Per Article VI § 6.4 — Tier 2 adds 99% SLA, bulk endpoints, quarterly methodology-sync calls. |
| **Renewal decision** | Both | 60 days before end of Initial Term, both Parties confirm renewal intent. Default is automatic 12-month rolling unless either Party gives non-renewal notice (Article IV § 4.2). |

---

## What DC Hub commits to building specifically for NLR

Beyond the standard Schedule A surface, these items are NLR-prompted and not in our public roadmap. They appear here because they address NLR's stated needs from the March 2026 deck.

| Endpoint / Feature | NLR-flagged need it addresses | Target | Schedule A amendment? |
|---|---|---|---|
| `/api/v1/zoning` (parcel-level) | Zoning/permitting gap (March deck) | 12 months | Yes — A.2 addition |
| `/api/v1/interconnection-queue/aging` | Demand queue times (March deck) | 90 days | Yes — A.1 addition |
| `/api/v1/reveal-validation-feed/<region>` | reVeal-specific validation rig support | 60 days | No — already A.5 |
| Cell-level forecast at 5.76km grid resolution | Matches reVeal's grid spec exactly | 90 days | Yes — A.3 enhancement |
| Quarterly snapshot — Parquet + GeoJSON + Apache Iceberg variant | Standardized research data format | Year 1 | No — Schedule B.5 already covers |
| MCP-native methodology query helper | Galen's research workflow | 30 days | No — MCP server enhancement |

---

## What NLR commits to providing DC Hub (reciprocal)

**These are the in-kind value items from MOU Schedule B § B.5 expressed as a delivery schedule.**

| Item | Owner | Timing |
|---|---|---|
| **Co-authorship on Stream B validation study** | NLR (Galen, Gabriel) | Author list locked at first manuscript review |
| **Factual reference rights** | NLR (Gabriel) | Live Day 1 — can list NLR as research user on `dchub.cloud/partners/nlr` once Article XII approval lands |
| **Joint conference / workshop presence** | Both | Mutually agreed venues; expected ≥1/year |
| **First-look on reVeal v2 outputs** | NLR | If reVeal v2 ships in the 24-month window |
| **Tier 2 only:** quarterly methodology-sync calls | NLR (Galen) + DC Hub | 1-hour Zoom, 4× per year if Tier 2 elected |

---

## Open product questions for the JSC kickoff call

The following are decisions/agreements we want to reach (or at least start) on the kickoff call. Pre-thought-through here so Jonathan can drive efficient conversation:

1. **Pilot region selection.** DC Hub recommendation: PJM (Mid-Atlantic corridor, includes Ashburn) for Stream A integration; ERCOT (Texas Triangle) as Stream B validation comparison. Anchored on highest buildout density and where reVeal's reserve-margin assumptions are most testable. Open to NLR counter-proposal.

2. **Validation rig priority limitation.** DC Hub recommendation: **transmission hosting capacity** (NLR's own #1 flagged improvement). Highest signal-to-noise for the validation paper.

3. **Validation cadence.** Recommendation: quarterly comparison reports during the engagement, vs. one end-of-study report. Quarterly aligns with the JSC review cadence and gives early-warning if the methodology drifts.

4. **Schedule A amendments.** Confirm process: JSC consensus is needed for new endpoint additions (e.g., `/api/v1/zoning`, `/interconnection-queue/aging`). Suggest: written request via JSC, 10-business-day review, mutual sign-off, added to the License in a Schedule A revision letter.

5. **MCP server adoption.** Optional but recommended. Galen or Ian configures the DC Hub MCP custom connector in Claude/Cursor/ChatGPT. Lets the validation rig query DC Hub data conversationally for ad-hoc methodology questions. Strong fit for academic exploration workflows.

6. **Stream C launch timing.** Public repo launches at 180 days — sooner if both Parties agree, slower if NLR Legal wants more time for review.

7. **Press strategy.** Joint press release for Validation Study publication is the natural anchor. Pre-publication launches (e.g., MOU signature announcement) require NLR JSC approval per Article XII § 12.4 — recommend skipping pre-execution PR entirely and saving for the paper.

---

## Dependencies on NLR

DC Hub commits to delivering the above on the timelines noted, but several items have NLR-side dependencies. The JSC should track these explicitly:

| DC Hub commitment | NLR-side dependency |
|---|---|
| Data Dictionary (30 days) | None |
| Path aliases (60 days) | None |
| First quarterly snapshot (60 days) | NLR provides delivery target (S3 bucket, DOE-approved transfer mechanism, etc.) |
| Validation rig prototype (90 days) | Galen confirms pilot region + priority limitation |
| Stream C scaffold (90 days) | None (private) |
| Stream C public launch (180 days) | NLR JSC approval of public copy + repo name |
| `/api/v1/zoning` (12 months) | NLR helps prioritize the top markets for initial coverage |
| reVeal v2 integration (24 months) | NLR ships reVeal v2 in the window AND provides schema in advance |

---

## What happens after Year 2

The Initial Term is 24 months. Beyond that, the engagement transitions to one of three modes:

1. **Renewal at Tier 1** (default, no action) — auto-renews 12-month rolling at $10K + CPI-U.
2. **Tier 2 election** — NLR's dedicated DC-siting funding lands; engagement upgrades to $25K/yr with 99% SLA + bulk endpoints + quarterly methodology-sync calls.
3. **Convert to dedicated research grant / CRADA** — if either Party prefers a different contracting vehicle (Cooperative Research and Development Agreement is the natural FFRDC mechanism), this MOU can be assigned to the new contract on mutual agreement.

The MOU's Article IV § 4.2 (auto-renewal) and Article IV § 4.4 (90-day for-convenience termination) provide flexibility on all three.

---

## How to update this roadmap

Roadmap is a living doc. **Quarterly JSC reviews are the canonical update point.** Material changes (Schedule A amendments, timeline shifts) require JSC consensus and a written addendum.

For minor updates (e.g., specifying which top-20 markets get parcel zoning coverage first), DC Hub updates this file via PR, NLR reviews, no formal addendum required.

Source of truth: `docs/NLR_PRODUCT_ROADMAP.md` in `github.com/azmartone67/dchub-backend`.
