# CBRE × DC Hub: A Bespoke Power Delivery Intelligence Layer

*Refined proposal — June 2026*

---

## Slide 1 — The Premise

**JLL and Cushman buy DCHawk for static research. CBRE has the chance to use DC Hub for something DCHawk structurally cannot deliver: a private, live competitive moat that compounds with every weekly cycle.**

- Gordon's H2 2025 Trends names power delivery as the #1 site-selection variable but quantifies it qualitatively. We have the live ISO queue layer across 22 grids, 2,032-deal M&A overlay, and 5,706 discovered facilities with derived AI-use classification.
- DCHawk is owned by Leeds Equity (PE roll-up, historically underinvests in R&D). DC Byte runs on analyst-paced manual validation. Neither has live grid feeds, shell-co transaction overlay, or an MCP-delivered agentic surface.
- This isn't a data subscription. It's a single-broker custom intelligence pipe — built for CBRE Research, invisible to the market.

---

## Slide 2 — The Custom Product: **Pre-RFP Power Delivery Briefing System (PRD-PDB)**

A private CBRE-only intelligence feed. Not co-branded. Not on the DC Hub website. Three delivery channels, one underlying engine.

- **What CBRE gets:** Friday 1-pager per pursuit market (25 markets to start) quantifying months-to-energization, constraining transmission segments, last week's utility filings, and which filings match known hyperscaler shell-co naming patterns. Plus a private MCP endpoint (`/cbre/pdb/<market>`) Gordon's team queries from Claude or Cursor. Plus Tuesday deal pre-mortem alerts within 24h of queue movements in CBRE-watched markets.
- **How it works:** Aggregator on top of existing DC Hub primitives — `/api/v1/interconnection-queue/snapshot`, grid intelligence layer, `discovered_facilities.derived_use`, `list_transactions`. Templated weekly PDF render, gated MCP route, CBRE-issued enterprise key. ~3 engineer-weeks to ship v0. Methodology PDF (4 pages, classifier features + confidence intervals + false-positive rates) lands on Gordon's desk before brief #1 — clears his Series 6/7/63/65 compliance instinct upfront.
- **Why JLL can't replicate in 12-24 months:** They'd need live queue scraping across 22 grids + 2,032-deal corpus with shell-co pattern recognition + an MCP delivery layer. DCHawk has none of the three; DC Byte has none. Acquisition path = 12+ months. Build path = 2+ years to data parity, longer for the deal corpus (must accumulate contemporaneously — cannot be backfilled).

**Neutrality preservation:** Methodology-footnote attribution only. CBRE Research deliverables cite "DC Hub Power Delivery data layer" in the methodology line — the same way office leasing reports cite CoStar. No co-branded marketing. No press release. Gordon retains full editorial discretion.

---

## Slide 3 — Crawl → Walk → Run (Applied to PRD-PDB)

**CRAWL (June–August 2026, free — pre-H2 publication window)**
- Week 1: 4-page methodology PDF delivered to Gordon, pre-cleared.
- Week 2-3: PRD-PDB v0 built for Gordon's 12 H2 2025 primary markets.
- Week 4: First Friday brief drops. Gordon + 2 named CBRE analysts only.
- Weeks 5-12: Iterate on Gordon's feedback. Add Sun Belt I-20 corridor (Austin, Charlotte, Alabama, Mississippi). **Goal: ≥3 PRD-PDB data points footnoted in H2 2026 Trends.**

**WALK (Sept 2026 – March 2027, $50K/yr)**
- Tuesday deal pre-mortem alerts go live.
- MCP endpoint opens to 10 named CBRE analysts.
- Booked under CBRE Research tooling line — below procurement-review threshold.
- **Goal: Two consecutive citation cycles (H2 2026 + H1 2027). That's lock-in.**

**RUN (April 2027+, $150–250K/yr)**
- Enterprise seat across CBRE DCS Research (Americas, EMEA, APAC, ~30 analysts).
- Custom market additions on Gordon's request (Quebec hydro, offshore wind PPA).
- Parallel ship: Pat Lynch's brokerage variant — same data, pre-RFP capacity briefs for active pursuits. Doubles the moat without dividing it.

---

## Slide 4 — What We Need From Pat + Gordon This Week

1. **One approval:** Gordon greenlights CRAWL — receive the methodology PDF + 4 weekly briefs, free, no procurement involvement, no money committed.
2. **One data input:** One anonymized broker brief from a recent CBRE transaction so DC Hub can build the first PRD-PDB cross-referenced against a real CBRE pursuit and demonstrate the queue-movement → shell-co match in context Gordon recognizes.
3. **One meeting:** 60 minutes with Gordon and 2 named analysts to walk through the methodology PDF and the v0 brief format. Denver-friendly (Gordon's based at 1225 17th).
4. **One clarification:** Confirm methodology-footnote attribution ("DC Hub Power Delivery data layer" in CBRE Research methodology line) is compatible with CBRE's neutrality posture. If yes, we ship. If footnote-only is too much, we propose internal-tooling-only attribution and revisit at WALK.
