# DC Hub × NLR — Partnership Roadmap (v2)

**Status as of 2026-05-26**
**Replaces v1.** Now grounded in the actual agreement package (4 PDFs in
legal review): Overview (00), MOU (02), Research Data License (03),
Publication Protocol (04). NDA (01) to be provided by NLR.

NLR = **National Laboratory of the Rockies** (operating for U.S. DOE).
Open-source reVeal package on GitHub: **NatLabRockies**.

---

## Contacts

| Role                  | Name              | Email                          | Status |
|-----------------------|-------------------|--------------------------------|--------|
| Strategic (JSC)       | Gabriel Zuckerman | Gabriel.Zuckerman@nlr.gov      | Active |
| Technical (JSC)       | Galen Maclaurin   | TBC @nlr.gov                   | Pending key |
| Integrator (added by Gabe) | Ian *(last name TBC)* | TBC                  | Pending key |
| DC Hub (JSC)          | Jonathan Martone  | azmartone@gmail.com            | — |

**Action**: confirm Galen and Ian's emails to mint Developer keys.

---

## Three work streams (per MOU Article II)

| Stream | Description | Governed by |
|--------|-------------|-------------|
| **A — Research Data Integration** | DC Hub provides NLR research-tier API access; NLR incorporates live data into reVeal's feature set. Pilot scope: 1–2 regions, single prioritized limitation (suggested: transmission hosting capacity). | Research Data License Agreement (Document 03) |
| **B — Joint Validation Research** | Peer-reviewed paper comparing reVeal projections vs DC Hub `discovered_facilities`. 6–9 months from CRADA execution. Working title: *"Validating Geospatial Data Center Buildout Projections with Real-Time Operational Signals — A reVeal × DC Hub Case Study, 2025–2028"* | Future CRADA / Joint Research Agreement |
| **C — Open-Method Extension** | Open-source tooling to export DC Hub feeds into reVeal ecosystem. License: Apache 2.0 or BSD-3-Clause. | Separate written agreement before substantial work |

---

## Commercial terms (per License Schedule B)

| Tier | What's included | Commercial rate | NLR rate |
|------|-----------------|-----------------|----------|
| **Tier 1 — Research** | All Schedule A endpoints, 95% SLA, business-day email, quarterly `discovered_facilities` snapshot | $100K/yr | **$10K/yr** (90% off) |
| **Tier 2 — Research Plus** | Tier 1 + 99% SLA + 4-hr priority + bulk endpoints (4 full-US grid exports/mo) + quarterly methodology-sync calls + custom-endpoint roadmap input | $250K/yr | **$25K/yr** (90% off) |

> ⚠️ **Action required before signature**: the Stripe Payment Link
> (https://buy.stripe.com/cNi3cwaNc0x75utdCqaZi0e) is sized at **$3K/yr**
> (Research Seed) which is **below the License Tier 1 NLR rate of $10K**.
> Three ways to reconcile:
> 1. Update License Schedule B to add a **Tier 0 — Research Seed** row at
>    $3K/yr (FY 2026 only, converts to Tier 1 in FY 2027).
> 2. Treat the $3K as a one-time invoicing concession with the Tier 1
>    rate in the License unchanged — document via side letter.
> 3. Keep Tier 1 at $3K for Year 1 and use the renewal-fee-adjustment
>    clause (Section 4.4) to step to $10K in Year 2.
>
> **Recommendation**: option 1 (add Tier 0 row). Keeps the License
> internally consistent and gives future research-seed partners (after
> NLR proves the model) a copy-paste tier.

### Renewal protection (License §4.4)
Fee adjustment on renewal: max of 5% or CPI-U change. Discount ratio
stays ≥ 90% off commercial rate. 60-day non-renewal notice.

### Cost-plus transparency (your email to Gabe, 2026-05-26)
- Year 1 cost to serve: ~$8K
- Year 1 Seed rate: $3K → **~-$5K strategic investment**
- Year 2+ Tier 1 rate: $10K → **~20% margin**
- Returns to sustainable cost-plus model by Year 2

---

## Document workstream status

| # | Doc | Binding? | Status | Notes |
|---|-----|----------|--------|-------|
| 00 | Overview | informational | ✅ Drafted | v2 — supersedes v1 |
| 01 | **NDA** (NLR's standard form) | **Fully binding** | ⏳ Awaiting NLR | Phase 1, this week. DC Hub counsel to redline so it permits pre-MOU technical exchanges. |
| 02 | **MOU / Framework Agreement** | Mostly non-binding (binds on IP, confidentiality, publications, co-marketing, term, misc) | ✅ Drafted | Phase 2, 2–4 weeks. Signed with Doc 04 attached as Schedule C. 24-month term. |
| 03 | **Research Data License Agreement v2** | **Fully binding** | ✅ Drafted | Phase 3, 6–8 weeks. 12-month auto-renew. 8 Schedules (A–H). |
| 04 | **Publication Protocol** | **Binding** when executed with MOU | ✅ Drafted | Schedule C to MOU. Includes honesty clause + 30-day review window. |
| —  | **CRADA / Joint Research Agreement** | TBD | ⏳ Phase 4 | When Validation Study ready. Uses NLR's standard CRADA template. |

### License Schedules (in Doc 03)

| Schedule | Title | What it does |
|---|---|---|
| A | Data Feeds and Endpoints | ~25 licensed endpoints across 5 categories (see next section). |
| B | Fee Schedule and In-Kind Value Exchange | Tier table + in-kind value flowing both ways. |
| C | Service Levels and Rate Limits | Tier 1: 95% SLA, 50 rps, 1M req/mo. Tier 2: 99% SLA, 200 rps, 10M req/mo. |
| D | Attribution Language | Citation form: *"Data provided by DC Hub (dchub.com) under a research license to ..."* ⚠️ **Flag**: should be `dchub.cloud`, not `dchub.com`. |
| E | Data Dictionary and Provenance | Per-endpoint methodology doc delivered within 60 days of effective date. |
| F | Security and Incident Response | TLS 1.2+, AES-256 at rest, NIST 800-53 alignment, 72-hour breach notification. FedRAMP / ISO 27001 status: **TBD**. |
| G | Change Management and Deprecation | Breaking change: 90-day notice. Deprecation: 180-day notice. Named-research-endpoint stability: **no breaking change within 12 months of publication submission**. |
| H | Acceptable Use Policy | Permitted: non-commercial research, validation, methodology dev, academic pubs. Prohibited: commercial redistribution, model training that substitutes DC Hub data, paid consulting on specific site decisions. |

---

## Schedule A — Endpoint surface (~25 endpoints)

**Status legend**: ✅ live · 🛠 in development per License A.5 · ❓ verify

### A.1 Grid and Interconnection (6)
- ✅ `/api/v1/grid-headroom`
- ✅ `/api/v1/grid-intelligence`
- ❓ `/api/v1/grid-data` *(was 404 in our probe — verify schema match)*
- ✅ `/api/v1/interconnection-queue`
- ✅ `/api/v1/infrastructure`
- ✅ `/api/v1/energy-prices` *(may be aliased to `/api/v1/energy/retail` — verify the License path matches the live path)*

### A.2 Siting Variables (6)
- ❓ `/api/v1/air-permitting` *(verify — code in `air_permitting_*.py` but not probed yet)*
- ✅ `/api/v1/tax-incentives`
- ✅ `/api/v1/water-risk` *(now registered via r73-a; License says `/water-risk`, code says `/water/stress` — align before signature)*
- ✅ `/api/v1/fiber-intel`
- ✅ `/api/v1/renewable-energy` *(aliased to `/api/v1/energy/renewable`)*
- ✅ `/api/v1/geothermal-potential`

### A.3 Composite Intelligence (NLR-aligned, 7)
- ✅ `/api/v1/reveal-cell`
- ✅ `/api/v1/colocation-score`
- ✅ `/api/v1/microgrid-viability`
- ✅ `/api/v1/intelligence-index`
- ✅ `/api/v1/analyze-site`
- ✅ `/api/v1/compare-sites`
- ✅ `/api/v1/dchub-recommendation`

### A.4 Market and Facility Data (5 + 1 snapshot)
- ✅ `/api/v1/facility`
- ✅ `/api/v1/search-facilities`
- ✅ `/api/v1/pipeline`
- ✅ `/api/v1/market-intel`
- ✅ `/api/v1/news` (used as social-acceptance proxy until A.5 lands)
- ✅ `/api/v1/list-transactions`
- ⏳ Quarterly Parquet/GeoJSON snapshot of `discovered_facilities` (manual delivery — set up cron)

### A.5 reVeal-Specific (6, in development per Initial Term)
- ✅ `/api/v1/reveal-cell-bulk` (in `reveal_endpoints.py`)
- ✅ `/api/v1/reveal-grid-export` + `/status/<job_id>` (async, in `reveal_endpoints.py`)
- ✅ `/api/v1/reveal-validation-feed`
- ✅ `/api/v1/social-acceptance-index`
- ✅ `/api/v1/climate-risk`
- ✅ `/api/v1/carbon-intensity`

> **All A.5 endpoints are already shipped in `routes/reveal_endpoints.py`** — they were marked "in development" in the License draft but the code is live. Update Schedule A copy to remove "in development" qualifier OR move them to Schedule A.4.

---

## Items requiring attention BEFORE signature

| # | Issue | Where | Fix |
|---|---|---|---|
| 1 | **Stripe $3K vs License Tier 1 $10K mismatch** | License Schedule B vs Stripe Payment Link | Add Tier 0 — Research Seed row at $3K (FY 2026 only) per recommendation above |
| 2 | **`dchub.com` vs `dchub.cloud`** | License Schedule D attribution language | Replace `dchub.com` → `dchub.cloud` in Schedule D + any other instances |
| 3 | **A.5 endpoints listed as "in development"** | License Schedule A.5 | All 6 are actually shipped today — update copy |
| 4 | **A.2 path naming**: `/water-risk` vs `/water/stress`, `/energy-prices` vs `/energy/retail`, `/renewable-energy` vs `/energy/renewable`, `/fiber-intel` vs `/fiber/intel` | License Schedule A | Either alias live paths to match License OR update License paths to match live code |
| 5 | **Bracketed placeholders** | All 4 docs | `[NLR Operating Entity Legal Name]`, `[STATE]`, `[ENTITY TYPE]`, `[JURISDICTION]`, `[FORUM]`, `[EFFECTIVE DATE]`, `[MOU DATE]`, `[REGION]`, `[FedRAMP / ISO 27001 status TBD]` |
| 6 | **DC Hub legal entity** | All 4 docs | Confirm DC Hub's legal entity name + state of formation (Delaware LLC?). Currently shown as "Martone Advisors, LLC · DC Hub" in proposal footer. |
| 7 | **DOE contract number** | License § Recitals, Pub Protocol § 3.4 | Confirm NLR's DOE prime contract number for inclusion in Acknowledgments. |
| 8 | **Security certification status** | License Schedule F | Decide whether to claim SOC 2 / FedRAMP / ISO 27001 alignment or explicitly defer. |
| 9 | **NLR Tier election** | License Schedule B | Confirm NLR elects Tier 1 (presumably — Tier 2's $25K exceeds stated budget). |
| 10 | **Counsel engagement** | Overview p3 | Engage startup attorney with FFRDC/data-licensing experience (10–20 hrs initial review). |

---

## Sequence — 90-day execution path

### Phase 1 — This week
- [ ] NLR sends mutual NDA (their standard form)
- [ ] DC Hub counsel reviews + countersigns (1-day target)
- [ ] Stripe subscription procured by NLR ($3K via PO, net-30)
- [ ] Galen + Ian emails → DC Hub mints Developer keys via `scripts/r72_onboard_reveal_nlr.sh`

### Phase 2 — Weeks 2–4
- [ ] MOU (02) + Publication Protocol (04) signed together (Schedule C attached)
- [ ] JSC kickoff meeting (Gabriel + Galen + Jonathan)
- [ ] Pilot scope confirmed in MOU Schedule A (regions + priority limitation = transmission hosting capacity)

### Phase 3 — Weeks 5–8
- [ ] Research Data License (03) red-line cycle
- [ ] Schedule fixes from "Items requiring attention" above
- [ ] Counsel review (DC Hub side + NLR side)
- [ ] License executed

### Phase 4 — Weeks 9–13 (per your email to Gabe)
- [ ] Initial validation results — water-availability layer swap with USGS data, RF retrain, AUC compare
- [ ] JSC Q1 strategic review scheduled
- [ ] CRADA / Joint Research Agreement drafted when validation study ready to formalize

---

## In-kind value exchange (per License Schedule B)

### NLR → DC Hub (material consideration)
- Co-authorship on the Validation Study
- Reference rights ("NLR is a research user of DC Hub data") — factual, not endorsement
- Joint conference and workshop presence at mutually agreed venues
- First-look access to reVeal v2 outputs and documentation
- Tier 2: quarterly methodology-sync calls
- DOE/NLR brand halo for DC Hub reference sales (subject to MOU Article VII)

### DC Hub → NLR
- Complimentary API + MCP server access (Tier 1 endpoint surface)
- Per-endpoint Data Dictionary documentation (delivered within 60 days)
- Technical integration support per Schedule C
- Quarterly `discovered_facilities` snapshots (Parquet / GeoJSON)
- Development effort toward the 6 A.5 reVeal-specific endpoints

---

## Strategic positions (per Overview p2)

1. **Pricing posture** — 90% discount is strategic. Counsel may push for commercial rates; **push back**. In-kind value clause in Schedule B makes this contractually explicit.
2. **Co-marketing constraint** — NLR can't endorse commercial products. Factual references only ("research user"), never promotional. Drafts already reflect this.
3. **Publication honesty** (Pub Protocol §4.3) — neither party can suppress findings unfavorable to its commercial / institutional interests. Essential for academic legitimacy + NLR's institutional credibility.
4. **Acquisition survivability** — License + MOU survive DC Hub acquisition. NLR retains 90-day without-cause termination right if acquirer materially changes the counterparty's nature.
5. **Endpoint roadmap** — Schedule A.5 (6 reVeal-specific endpoints) converts the relationship from "DC Hub as vendor" → **"DC Hub as co-developer of reVeal v2"**. Strategic value > monetary terms.

---

## Operational levers (admin-keyed via DCHUB_ADMIN_KEY)

| If you need to | Run |
|----------------|-----|
| Mint a Developer key for new NLR contact | `CONTACT_EMAIL=… CONTACT_NAME="…" ./scripts/r72_onboard_reveal_nlr.sh` |
| Upgrade Developer → Enterprise (post-License execution) | Same script with `PLAN=enterprise` prefix |
| Read full deal context per key | `curl -H "X-Admin-Key: $DCHUB_ADMIN_KEY" .../api/v1/admin/partner-key/audit \| jq '.keys[] \| select(.partner_slug=="reveal-nlr")'` |
| Revoke a compromised key (1 business hour SLA per Schedule C.3) | `curl -X POST -H "X-Admin-Key: $DCHUB_ADMIN_KEY" .../api/v1/admin/partner-key/revoke/<key_prefix>` |

---

## Open questions for Gabe / NLR Legal

1. Galen Maclaurin's email (assume `Galen.Maclaurin@nlr.gov`?)
2. Ian's last name + email
3. NLR's exact operating entity legal name (for `[NLR Operating Entity Legal Name]` placeholder)
4. DOE prime contract number (for Acknowledgments + License recitals)
5. NLR's preferred governing-law jurisdiction (Delaware works for DC Hub; federal law applies to NLR-specific provisions)
6. Target journal/venue for the Validation Study (impacts License G.3 stability commitment timeline)
7. NLR security contact + billing contact + PO procurement contact (License Schedule F.2, B.5)
8. Tier election: Tier 1 ($10K) or Tier 2 ($25K)? Recommendation: Tier 1 for Year 1 + upgrade path to Tier 2 if dedicated funding lands.

---

## File pointers

| Doc | Path |
|-----|------|
| Overview (00) | `~/Downloads/.../DCHub_NLR_Agreement_00_Overview.pdf` |
| MOU (02) | `~/Downloads/.../DCHub_NLR_Agreement_02_MOU_Framework.pdf` |
| License (03) | `~/Downloads/.../DCHub_NLR_Agreement_03_Research_License.pdf` |
| Pub Protocol (04) | `~/Downloads/.../DCHub_NLR_Agreement_04_Publication_Protocol[42].pdf` |
| Sales proposal | `~/Downloads/DCHub_NLR_Enterprise_License_Proposal copy.pdf` |
| Original Partnership doc | `~/Downloads/DCHub_reVeal_Partnership.pdf` |
| NLR landing page (HTML) | https://dchub.cloud/partners/nlr *(once CF Pages deploy unsticks)* + https://dchub-backend-production.up.railway.app/partners/nlr *(Railway direct, works today)* |
| Engineering deliverables | `routes/nlr_intelligence.py`, `routes/reveal_endpoints.py`, `routes/reveal_cell.py` |
| Onboarding script | `scripts/r72_onboard_reveal_nlr.sh` |
| Stripe Payment Link | https://buy.stripe.com/cNi3cwaNc0x75utdCqaZi0e |
