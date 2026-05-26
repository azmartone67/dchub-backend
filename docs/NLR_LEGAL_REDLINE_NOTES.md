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

**Where:** Document 03 (License v2), Schedule A.5 (reVeal-Specific Endpoints)

**Issue:** A.5 currently marks six reVeal-specific endpoints as
"in development" for delivery during the Initial Term. All six are
already shipped in production today and exercised by NLR partner keys.

**Endpoints to reclassify from "in development" to "live":**

| Endpoint | Live since | Code location |
|---|---|---|
| `/api/v1/reveal-cell-bulk` | 2026-Q1 | `routes/reveal_endpoints.py` |
| `/api/v1/reveal-grid-export` + `/status/<job_id>` | 2026-Q1 | `routes/reveal_endpoints.py` |
| `/api/v1/reveal-validation-feed` | 2026-Q1 | `routes/reveal_endpoints.py` |
| `/api/v1/social-acceptance-index` | 2026-Q1 | `routes/reveal_endpoints.py` |
| `/api/v1/climate-risk` | 2026-Q2 | `routes/api_integration_wiring.py` |
| `/api/v1/carbon-intensity` | 2026-Q2 | `routes/api_integration_wiring.py` |

**Proposal:** Move these six rows from A.5 ("in development") into
A.4 ("Market and Facility Data") or relabel A.5 as
"reVeal-Specific (live as of License effective date)".

**Rationale:** "In development" creates an implicit delivery-risk clause
that has already been satisfied. NLR partner keys exercise these
endpoints today — verifiable via OpenAPI spec at
`dchub.cloud/openapi.json`.

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
