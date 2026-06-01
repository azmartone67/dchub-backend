# DC Hub × CBRE — Scoping Document & Memorandum of Understanding

*Draft · v0.1 · prepared for review by both sides post-introductory call*

---

## 1. Parties

- **DC Hub** — Data Center Intelligence Platform, dchub.cloud, Switzerland-
  model neutral data primitive. Founder: Jonathan Martone.
- **CBRE Data Center Solutions (DCS)** — CBRE's data center capital
  markets + advisory practice. Lead contact: Pat Lynch. Technical
  lead: Gordon [last name].

---

## 2. Purpose

DC Hub and CBRE intend to explore a bilateral relationship in which:

- DC Hub's live-refresh data layer (DCPI verdicts, 21,419 facility
  catalog, $324B+ M&A tracker, 10-ISO interconnection intelligence)
  makes CBRE's analytical authority faster and more defensible against
  AI-era research expectations.
- CBRE's broker-validated market intelligence and client relationships
  make DC Hub a more defensible standard in the data center asset
  class.

This is **not** an exclusive arrangement. DC Hub operates under a
publicly-published "Switzerland model" framework
(https://dchub.cloud/news/partnership-cbre-2026-w22) in which every
qualified broker, advisory firm, and industry data provider receives
the same baseline terms.

---

## 3. Structure (one of three — to be selected post-call)

**[ ] Structure A — Co-Authored Research Publication**
- DC Hub provides live-data layer + methodology appendix for the
  CBRE H2 2026 Data Center Trends Report.
- CBRE provides analytical narrative, distribution, brand authority.
- Both logos on cover; joint webinar at launch.
- **$0 upfront from either side.** Revenue share: 50/50 on enterprise
  data licenses where the joint report is cited as buyer's source of
  awareness, capped at $100K per side per year.

**[ ] Structure B — Data Exchange Pilot**
- 30-day mutual data-quality pilot per the published framework.
- DC Hub commits to exposing 500 of its highest-confidence facilities
  in CBRE's coverage gap. CBRE commits to exposing 500 reciprocal
  records in DC Hub's gap.
- Both sides measure join-rate + incremental coverage lift.
- **No money changes hands.** Written MOU (Structure A or C) triggers
  only if both sides see ≥15% lift on the joined dataset.

**[ ] Structure C — Strategic Alliance**
- CBRE purchases DC Hub Strategic Partner tier ($250K/yr, 3-year term
  with annual reviews).
- Includes: custom DCPI weights tuned to CBRE investment thesis;
  unlimited CBRE-side seats; co-branded research distribution rights;
  raw exports (parquet, CSV); quarterly executive briefing with DC
  Hub founder.
- Referral fee structure: 10% revenue share on inbound deals where
  DC Hub data sourced the lead (mutual, both directions).
- Joint press release at signing.

---

## 4. Mutual commitments (applies regardless of structure chosen)

### DC Hub commits to:

- **Daily-refresh DCPI** across 234+ markets (current count) with
  full audit trail to underlying EIA / HIFLD / ISO inputs.
- **API SLA** of 99.5% monthly availability; published incident log.
- **Methodology transparency** — every DCPI score links to its inputs
  on request.
- **Data corrections process** — autonomous brain detects anomalies;
  material corrections published within 72 hours of identification.
- **Non-competitive posture** — DC Hub does not broker deals, does not
  represent buyers or sellers, does not own facilities. We remain a
  data primitive layer.

### CBRE commits to:

- **Good-faith engagement** in the pilot / partnership work product.
- **Co-attribution** — when DC Hub data appears in CBRE publications,
  the attribution "Source: DC Hub" (or equivalent agreed phrasing)
  appears alongside.
- **No claim to exclusivity** — CBRE acknowledges DC Hub continues to
  offer the same baseline framework to other qualified counterparties.
- **Privacy of underlying client data** — any CBRE client information
  shared with DC Hub during the pilot remains confidential per Section 7.

---

## 5. Timeline (90-day path to formal partnership)

| Day | Milestone | Owner |
|---|---|---|
| 0 (today) | Introductory call — structure selection, stakeholders named | Both |
| +3 | DC Hub sends this scoping doc + draft data-exchange schema | DC Hub |
| +7 | CBRE legal completes first review | CBRE |
| +10 | Mutual NDA signed (Structures B/C — A doesn't require one) | Both |
| +14 | Technical handshake — DC Hub provisions Pro API access for CBRE technical lead | DC Hub |
| +21 | Pilot kickoff call (3-5 people each side, success metrics agreed) | Both |
| +45 | Mid-pilot check — share preliminary lift/coverage numbers | Both |
| +60 | Pilot review meeting — go/no-go decision on formal MOU or commercial license | Both |
| +75 | Final MOU + statement of work signed (or Structure A: joint authorship begins) | Legal |
| +90 | Joint press release / LinkedIn announcement | Marketing |

Either side may terminate at any milestone with 7 days' written notice
and no penalty during the 90-day exploration window.

---

## 6. Stakeholders

### DC Hub
- **Jonathan Martone** — Founder; strategic + technical lead
- **(Add when team grows)** — Reserved

### CBRE
- **Pat Lynch** — DCS lead, primary strategic contact
- **Gordon [last name]** — Technical lead, primary data/methodology contact
- **(Add)** — H2 report owner (Structure A only)
- **(Add)** — Legal review contact
- **(Add)** — Procurement contact (Structure C only)

---

## 7. Confidentiality

Both parties agree that any non-public information shared during the
90-day exploration window is treated as confidential and used only
for the purposes outlined here. Confidentiality survives termination
of this scoping engagement for 2 years.

DC Hub publishes aggregate platform metrics (lifetime AI requests,
citation counts, market coverage) publicly as part of its operating
model. **No CBRE-specific data appears in any DC Hub public surface
without CBRE's explicit written approval.**

---

## 8. Non-binding nature

This Scoping Document is **non-binding** and creates no commercial
obligation on either side. It is intended to align expectations and
sequence the work that would precede a binding agreement (the formal
MOU at +75 days). Either side may walk away at any milestone with
written notice.

---

## 9. Public framing

Both parties agree the following statement (or one mutually agreed)
governs any pre-MOU external communication:

> *"DC Hub and CBRE Data Center Solutions are in a 90-day exploration
> of a bilateral data-exchange and methodology-alignment framework.
> No commercial commitments have been made. The framework is part of
> DC Hub's publicly-published Switzerland-model partnership program
> open to qualified industry data + advisory firms."*

This is the language we'd use if either side is asked publicly about
the relationship during the exploration window.

---

## 10. Signatures

This scoping document is signed by both parties to acknowledge
alignment on the path forward — not to commit to specific commercial
terms.

| | |
|---|---|
| **Jonathan Martone** | Pat Lynch |
| Founder, DC Hub | DCS Lead, CBRE |
| Signature: ____________________ | Signature: ____________________ |
| Date: ___________ | Date: ___________ |

---

## Appendix A — DC Hub current platform metrics (as of MOU draft date)

These metrics are live at the time of drafting and would refresh on
signature. CBRE may verify any number against the live source URL
listed.

| Metric | Value | Source |
|---|---|---|
| Facilities tracked | 21,419 | https://dchub.cloud/api/v1/agents/capabilities.json |
| DCPI markets scored | 234 | https://dchub.cloud/dcpi |
| M&A deals tracked | 2,232 ($324B+ value) | https://dchub.cloud/transactions |
| Substations | 126,439 | EIA + HIFLD merged |
| Transmission lines | 56,108 | HIFLD |
| Lifetime AI-agent requests | 439,768 | https://dchub.cloud/api/v1/mcp/funnel |
| Annualized AI request run-rate | 1,736,384 / year | 7-day extrapolation |
| Active dev keys | 47 (44 free + 2 paid + 1 enterprise) | mcp_dev_keys |
| AI platforms citing | Claude, ChatGPT, Gemini, Copilot, Perplexity, Grok + 96 more | https://dchub.cloud/api/v1/agents/citations.json |

---

## Appendix B — Switzerland framework (DC Hub's pre-existing public commitment)

DC Hub published its open partnership framework on 2026-05-26:

> *"DC Hub publishes today an open partnership framework outlining how
> every major broker, advisory firm, and industry data provider can
> engage with the platform under a Switzerland model — neutrality, no
> exclusivity, no channel conflict."*

Named in the public invitation (in addition to CBRE): JLL Data Center
Group, Cushman & Wakefield, Newmark, Datacenter Dynamics (DCD), DCByte,
DataCenterHawk, Structure Research, Synergy Research Group, and "other
industry data and advisory firms."

CBRE acknowledges that engagement with DC Hub under this scoping
document does not foreclose DC Hub's continuing engagement with the
above-named firms or future qualified counterparties.

---

*End of scoping document. To activate, both signatories complete
Section 10 and email signed copies to press@dchub.cloud and
[pat.lynch@cbre.com]. DC Hub will then log the engagement in its
enterprise_inquiries pipeline and assign Day-0 of the 90-day timeline.*
