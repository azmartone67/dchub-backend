# DC Hub Counter-Redline Notes — NLR Partnership Agreement Package

**Version 1 — 2026-05-26**
**For:** Gabriel Zuckerman (NLR), NLR Legal Counsel
**From:** Jonathan Martone, DC Hub
**Documents covered:** 02 MOU Framework · 03 Research Data License v2 · 04 Publication Protocol
*(00 Overview is informational only; 01 NDA awaiting NLR's standard form)*

---

## Summary

This document itemizes DC Hub's proposed changes to the NLR agreement
package prior to execution. Twelve items are grouped by ownership:

- **A. DC Hub redlines (4)** — specific text replacements we are proposing
- **B. NLR information requests (4)** — placeholders we need NLR to fill
- **C. Bilateral decisions (4)** — items where we propose a position and both sides agree

### Recommended execution sequence

| Phase | Timing | Documents | Owner |
|---|---|---|---|
| 1 | This week | NDA (01) — NLR's standard form | NLR sends, DC Hub countersigns |
| 2 | Weeks 2–4 | MOU (02) + Pub Protocol (04) co-execute | Both, w/ Phase 2 redlines settled |
| 3 | Weeks 5–8 | License (03) | Both, w/ Phase 3 redlines settled |
| 4 | Weeks 9–13 | CRADA / Joint Research Agreement | After validation study scopes |

---

## A. DC Hub Redlines

### A1. License Schedule B — add **Tier 0 — Research Seed** row

**Where:** Document 03 (License v2), Schedule B (Fee Schedule and In-Kind Value Exchange)

**Issue:** NLR has executed a $3K/yr Stripe subscription
(`buy.stripe.com/cNi3cwaNc0x75utdCqaZi0e`). Current License Tier 1 rate
is $10K/yr. Without a Tier 0 row, the License is internally inconsistent
with the executed payment.

**Proposed Schedule B fee table:**

| Tier | Endpoints | SLA | Commercial rate | NLR rate |
|---|---|---|---|---|
| **Tier 0 — Research Seed** *(new)* | All Schedule A endpoints | 95% | $30K/yr | **$3K/yr** *(FY 2026 only)* |
| Tier 1 — Research | All Schedule A endpoints | 95% | $100K/yr | $10K/yr |
| Tier 2 — Research Plus | + 99% SLA + bulk + quarterly methodology sync | 99% | $250K/yr | $25K/yr |

**Proposed footnote on Tier 0:**
> "Tier 0 — Research Seed is an introductory rate available for FY 2026
> only. Year 2 (FY 2027) automatically transitions to Tier 1 at the
> then-current Tier 1 NLR rate, subject to the renewal-fee-adjustment
> mechanism in §4.4. The Stripe Payment Link referenced in Schedule B
> reflects the Tier 0 rate."

**Rationale:** Keeps the License internally consistent with the executed
payment and gives NLR a clean Year 2 transition into the standard Tier 1
research rate. Also gives future research-seed partners (after NLR
proves the model) a copy-paste tier.

---

### A2. License Schedule D — attribution domain correction

**Where:** Document 03 (License v2), Schedule D (Attribution Language)

**Issue:** The proposed attribution language references `dchub.com`,
which is **not** DC Hub's operating domain.

**Replace:**
> *"Data provided by DC Hub (**dchub.com**) under a research license to [NLR Operating Entity Legal Name]"*

**With:**
> *"Data provided by DC Hub (**dchub.cloud**) under a research license to [NLR Operating Entity Legal Name]"*

**Rationale:** `dchub.cloud` is DC Hub's live production domain (frontend
on Cloudflare Pages, API on Railway, MCP server at `dchub.cloud/mcp`).
`dchub.com` is not operated by DC Hub and the URL will not resolve to
our data products. Cited attributions in NLR's publications and reVeal
exports must point to the live domain.

---

### A3. License Schedule A.5 — strip "in development" qualifier

> **⚠️ CORRECTED 2026-08-01 — DO NOT TABLE THIS ITEM AS ORIGINALLY WRITTEN.**
> The v1 draft of A3 asserted that all six endpoints were "already shipped in
> production today and exercised by NLR partner keys." Verified against the
> live backend, the read replica and the API call log on 2026-08-01, that
> assertion was **not accurate** — one endpoint returned no data at all, one
> returns undeliverable download links, and the usage claim overstates what
> the logs show. Corrected facts below. **The proposal is retained but its
> supporting argument is now materially narrower; counsel should re-read
> before tabling.** Superseded text is preserved in git history
> (`docs/NLR_LEGAL_REDLINE_NOTES.md`, prior to PR #2074).

**Where:** Document 03 (License v2), Schedule A.5 (reVeal-Specific Endpoints)

**Issue:** A.5 currently marks six reVeal-specific endpoints as
"in development" for delivery during the Initial Term. All six are
**registered and return HTTP 200 in production**, but "shipped" is doing
more work than the evidence supports for four of them — see the status
column below.

**Endpoint status, verified 2026-08-01:**

| Endpoint | Serving status | Code location |
|---|---|---|
| `/api/v1/reveal-cell-bulk` | **Live** — computes per-cell results via `reveal_cell.compute_reveal_cell` | `reveal_endpoints.py` |
| `/api/v1/reveal-grid-export` + `/status/<job_id>` | **Live endpoint, undeliverable product** — see note 1 | `reveal_endpoints.py` |
| `/api/v1/reveal-validation-feed` | **Live since 2026-08-01 only** — returned an empty list on every call before that; see note 2 | `reveal_endpoints.py` |
| `/api/v1/social-acceptance-index` | **Live, heuristic** — 12 hard-coded jurisdictions, not live data; see note 3 | `reveal_endpoints.py` |
| `/api/v1/climate-risk` | **Live, heuristic** — 20 hard-coded zones; returns 0 outside them; see note 3 | `reveal_endpoints.py` |
| `/api/v1/carbon-intensity` | **Live, reference table** — static EIA/eGRID 2024 values by state | `reveal_endpoints.py` |

All six are in `reveal_endpoints.py` at the repository root. The v1 table's
code locations were wrong for every row: four cited a `routes/` prefix that
does not exist for this file, and `climate-risk` / `carbon-intensity` were
attributed to `routes/api_integration_wiring.py`, which does not contain them.

**Note 1 — `reveal-grid-export` hands back links that cannot be fetched.**
The handler returns `"status": "ready"` with
`download_url: https://cdn.dchub.com/grid-exports/<STATE>/2026-04-20/…`.
`cdn.dchub.com` resolves but does not serve — TLS fails with
`unrecognized name`. The 15-state availability list is annotated in-code as
"assumed to have nightly pre-renders," and its `last_refresh` is the hard-coded
literal `2026-04-20T06:00:00Z` for every state. `/status/<job_id>` returns
`"status": "ready"` plus a download URL for **any** job id, including one
invented for this check. No export artifact is produced.

**Note 2 — `reveal-validation-feed` had never returned a row.**
Its query named five columns that do not exist on `discovered_facilities`
(`lat`, `lng`, `nameplate_mw`, `announcement_date`, `updated_at`). Every call
raised `UndefinedColumn`; a bare `except Exception` downgraded it to a log
warning and the handler returned HTTP 200 with `"facilities": []`. Fixed and
deployed 2026-08-01 (PR #2073). The default 30-day window now matches ~1,292
facilities and returns the first 500 of them (`limit` defaults to 500, capped
at 5,000). Note the fix reports `announcement_date` as **null** —
that field has no source on this table, and the License should not be read as
promising it.

**Note 3 — three endpoints answer from static tables, not live data.**
`social-acceptance-index` (12 jurisdiction tuples), `climate-risk` (20 zone
tuples) and `carbon-intensity` (per-state reference values) return designed
heuristics. This is defensible as a modelling input, but their `source`
strings read as live-data attributions — `climate-risk` cites "FEMA flood +
NIFC wildfire + NOAA extreme heat proxies" while calling none of those
services. `climate-risk` also returns `composite: 0` / "Minimal" for any
location outside its 20 zones — Loudoun County VA scores 0 on all three
components — which is indistinguishable from a measured finding of no risk.
If NLR validates against these, that behaviour should be disclosed.

**Usage evidence — the "exercised by NLR partner keys" claim.**
`api_endpoint_log` covers 2026-06-03 → 2026-08-01 (853,521 calls across 1,554
endpoints). Every reVeal-family call in that window landed on a **single day,
2026-06-10, from a single API key prefix**:

| Endpoint | Calls | Distinct keys | First | Last |
|---|---|---|---|---|
| `/api/v1/reveal-cell-bulk` | 783 | 1 | 2026-06-10 | 2026-06-10 |
| `/api/v1/reveal-grid-export` | 13 | 1 | 2026-06-10 | 2026-06-10 |
| `/api/v1/reveal-cell` | 2 | 1 | 2026-06-10 | 2026-06-10 |
| `/api/v1/reveal-validation-feed` | 2 | 1 | 2026-06-10 | 2026-06-10 |
| `/api/v1/reveal-grid-export/status/…` | 2 | 1 | 2026-06-10 | 2026-06-10 |
| `/api/v1/carbon-intensity` | 1 | 1 | 2026-06-04 | 2026-06-04 |
| `/api/v1/social-acceptance-index` | **0** | — | — | — |
| `/api/v1/climate-risk` | **0** | — | — | — |

2026-06-10 is the date of the NLR meeting deck
(`docs/NLR_MEETING_2026-06-10_DECK.pptx`), so this pattern is consistent with
a single demonstration session rather than ongoing partner use. Two of the six
endpoints have never been called. **Caveat:** the log begins 2026-06-03 and
cannot confirm or refute usage during Q1/Q2, so "never used" is not a claim
this evidence supports — only "not used in the last eight weeks."

**Proposal (unchanged in direction, narrowed in support):** Relabel A.5 as
"reVeal-Specific (live as of License effective date)" rather than moving the
rows into A.4. Before tabling, decide:

1. Whether `reveal-grid-export` should be represented as live at all while its
   download URLs do not resolve, or carved out with a delivery commitment.
2. Whether the three heuristic endpoints are described accurately enough that
   "live" is not read as "live data."

**Rationale:** "In development" creates an implicit delivery-risk clause.
For `reveal-cell-bulk` that clause is satisfied. For the rest, the corrected
record above should be settled internally before DC Hub argues the clause has
been discharged — the v1 argument rested on facts that did not hold, and
tabling it as written would put an inaccurate representation in front of NLR
counsel.

**Engineering follow-ups (tracked separately from this negotiation):**
`routes/partner_landing.py:106` makes the same claim publicly — "10
reVeal-specific endpoints already shipped" — and should be reconciled with
the table above.

---

### A4. License Schedule A — endpoint path naming alignment

**Where:** Document 03 (License v2), Schedule A (all sub-sections)

**Issue:** A handful of License paths use a hyphenated form while live
production code uses a nested form (or vice versa). To avoid an
attribution-fails-to-resolve scenario in NLR publications, the License
paths and the live paths must match exactly.

**Path table (License draft → live code):**

| License path | Live path | Resolution |
|---|---|---|
| `/api/v1/grid-data` | `/api/v1/grid/data` | DC Hub will **add server-side alias** so both resolve. |
| `/api/v1/energy-prices` | `/api/v1/energy/retail` | DC Hub will **add server-side alias** so both resolve. |
| `/api/v1/renewable-energy` | `/api/v1/energy/renewable` | DC Hub will **add server-side alias** so both resolve. |
| `/api/v1/water-risk` | `/api/v1/water/stress` | DC Hub will **add server-side alias** so both resolve. |
| `/api/v1/fiber-intel` | `/api/v1/fiber/intel` | DC Hub will **add server-side alias** so both resolve. |
| `/api/v1/air-permitting` | *(verify — code in `air_permitting_*.py`)* | DC Hub will verify and confirm endpoint is live before License execution. |

**DC Hub commitment:** Aliases land in production within 60 days of
License effective date. License paths as drafted remain the canonical
citation form.

**Rationale:** Citation stability per Schedule G.3 — "no breaking change
within 12 months of publication submission" — depends on the cited paths
resolving. We add aliases (cheap, non-breaking) so both forms work.

---

## B. NLR Information Requests

The following fields are bracketed placeholders in the agreement package.
NLR Legal please provide before signature.

### B1. NLR operating entity legal name
**Used in:** all 4 documents — `[NLR Operating Entity Legal Name]`
**Need:** Exact legal name, entity type, jurisdiction of formation
(e.g., "National Laboratory of the Rockies, a federally funded research
and development center operated by [Operator] under DOE contract
[Number]").

### B2. DOE prime contract number
**Used in:** License Recitals, Publication Protocol §3.4 Acknowledgments
**Need:** NLR's current prime contract number with the U.S. Department
of Energy. Required for Acknowledgments paragraph in any joint
publication output.

### B3. NLR signatory + governing-law jurisdiction
**Used in:** MOU and License signature blocks
**Need:**
- Name + title of NLR signatory authorized to execute partnership agreements
- Preferred governing-law jurisdiction for the License *(DC Hub default: Delaware; federal law applies to NLR-specific provisions)*

### B4. NLR contacts (security, billing, PO procurement)
**Used in:** License Schedule F.2 (Security and Incident Response) and
Schedule B.5 (Invoicing)
**Need:**
- NLR security contact for incident notification (License F.2, 72-hr breach window)
- NLR billing contact for invoicing
- NLR procurement contact for PO and PO-amendment workflow

---

## C. Bilateral Decisions

### C1. Tier election (Tier 1 vs Tier 2)
**Where:** License Schedule B
**DC Hub recommendation:** Elect **Tier 1 ($10K/yr** under Tier 0 row at
$3K for FY 2026) with explicit upgrade path to Tier 2 ($25K/yr) if NLR's
dedicated DC-siting research funding closes.
**Why:** Tier 2's $25K exceeds NLR's stated FY 2026 budget. Tier 1
endpoint surface covers the validation paper scope. Tier 2's bulk
endpoints (4 full-US grid exports/mo) become useful when NLR begins
multi-region reVeal validation in Year 2+.

### C2. Security certification language
**Where:** License Schedule F (Security and Incident Response)
**Current draft language:** Claims "NIST 800-53 alignment, TLS 1.2+,
AES-256 at rest, 72-hour breach notification".
**DC Hub current operational state:**
- TLS 1.2+ across all endpoints ✓
- AES-256 at rest (Cloudflare + Railway + Neon Postgres) ✓
- NIST 800-53 framework alignment ✓
- **No active third-party SOC 2 / FedRAMP / ISO 27001 audit certification** at License execution
- DC Hub commits to begin SOC 2 Type 1 cycle in FY 2027 if NLR research
  output requires it (and to fund the cycle from the Year-2 Tier 1 fee)

**Proposed Schedule F language change:**
Replace any claim of active certification with: "DC Hub maintains
operational security aligned with NIST 800-53 controls. DC Hub commits
to begin a SOC 2 Type 1 audit cycle in FY 2027 if NLR or its
publication venue requires third-party certification. No active
SOC 2 / FedRAMP / ISO 27001 certification exists as of License
effective date."

**Rationale:** Honest claim of current state. Avoids representation
risk if NLR or its institutional reviewers audit the cert claim.

### C3. DC Hub legal entity name
**Where:** All 4 documents — DC Hub party signature block
**Current draft:** "DC Hub" or "Martone Advisors, LLC · DC Hub"
**DC Hub action:** Confirm exact d/b/a or filed entity name. Likely
"Martone Advisors, LLC, a [STATE] limited liability company, doing
business as DC Hub". Final form provided before signature.

### C4. Counsel engagement protocol
**Where:** Overview p3 (DC Hub-side commitment)
**DC Hub action:** Engaging startup counsel with FFRDC / federal
data-licensing experience for 10–20-hour redline review of the
package.
**NLR proposal request:** Confirm whether NLR Legal prefers a
serial redline cycle (NLR → DC Hub → NLR → execution) or parallel
review (both counsels redline simultaneously; merged version
finalized in a 30-min call).

---

## Reference: live endpoint surface

OpenAPI spec: `https://dchub.cloud/openapi.json`

All ~25 Schedule A endpoints verifiable via:

```
curl -H "X-API-Key: <NLR-developer-key>" \
  "https://dchub.cloud/api/v1/site-forecast?lat=39.04&lon=-77.48&state=VA"
```

Schedule A coverage by category (post-A4 path alignment):

| Schedule | Category | Live count | Notes |
|---|---|---|---|
| A.1 | Grid and Interconnection | 6 | All live |
| A.2 | Siting Variables | 6 | All live (1 to verify per A4 above) |
| A.3 | Composite Intelligence (reVeal-aligned) | 7 | All live |
| A.4 | Market and Facility Data | 5 + quarterly snapshot | All live |
| A.5 | reVeal-Specific | 6 | All live (per A3 redline above) |
| **Total** | | **~25 endpoints + 1 snapshot** | |

---

## Working paper title (Validation Study)

For Publication Protocol §2.1 (Title and Scope):

> *"Validating Geospatial Data Center Buildout Projections with
> Real-Time Operational Signals — A reVeal × DC Hub Case Study,
> 2025–2028"*

Authorship per Publication Protocol Schedule A: Galen Maclaurin (NLR),
Jonathan Martone (DC Hub), Gabriel Zuckerman (NLR) — order TBC by JSC
at kickoff.

---

## Open methodology questions (for JSC kickoff agenda)

1. **Pilot region(s)** — DC Hub suggestion: PJM (Ashburn corridor)
   + ERCOT (Texas Triangle). NLR to confirm based on reVeal output
   saturation.
2. **Priority limitation** — DC Hub suggestion: transmission hosting
   capacity (highest-signal validation against reVeal's reserve-margin
   layer). NLR to confirm or counter-propose.
3. **Validation cadence** — quarterly comparison reports vs. one
   end-of-study report?
4. **Journal/venue target** — affects Publication Protocol §G.3
   endpoint-stability commitment timeline (12 months from submission).

---

## File pointers

| Item | Path |
|---|---|
| This document | `docs/NLR_LEGAL_REDLINE_NOTES.md` |
| Partnership roadmap (internal) | `docs/NLR_PARTNERSHIP_ROADMAP.md` |
| Stripe Payment Link (executed) | `https://buy.stripe.com/cNi3cwaNc0x75utdCqaZi0e` |
| Onboarding script | `scripts/r72_onboard_reveal_nlr.sh` |
| Partner-key admin endpoints | `routes/partner_key_issuer.py` |

---

**Next action (DC Hub):** Send this document to Gabe with the original
4 PDFs (so his counsel has the full package), and queue the four
A1–A4 changes for incorporation into a revised License draft as soon
as Gabe's side confirms or counters each.

**Next action (NLR):** Provide B1–B4 information and respond to C1–C4
positions. JSC kickoff to schedule once NDA executes.
