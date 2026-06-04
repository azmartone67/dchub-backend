# The CBRE Asymmetric Moat: Pre-RFP Power Delivery Briefing System (PRD-PDB)

## The Chosen Moat

**One product. One name. One asymmetric advantage.**

DC Hub builds a private, CBRE-only **Pre-RFP Power Delivery Briefing System** — a Monday-morning 1-pager auto-generated for each CBRE pursuit market that quantifies what Gordon's H2 2025 report leaves qualitative: **how many months to energization, which transmission segments are constraining, which utility filings dropped last week, and which of those filings match known hyperscaler shell-co naming patterns.**

It is delivered three ways:
1. **Friday PDF brief** to Gordon's distribution list — 25 markets × 1 page, CBRE-internal cover, DC Hub in a methodology footnote only.
2. **Private MCP endpoint** (`/cbre/pdb/<market>`) Gordon's team can query from Claude/Cursor for any client mandate, returning the same data structured.
3. **Tuesday "deal pre-mortem" alert** when a new utility filing or queue position movement is detected in any CBRE-watched market — pushed to Gordon and a named CBRE analyst within 24h of source publication.

The product is **not co-branded, not announced, not on the DC Hub website**. It is a private feed. CBRE preserves its neutrality posture because nothing publishes externally; DC Hub appears only as a footnote in internal CBRE research deliverables on Gordon's discretion.

## Why It Aligns With Gordon's Priorities

Gordon has publicly named his own gap. From the H2 2025 North America Data Center Trends: power delivery is the #1 site-selection variable, and from his Construction Dive feature: *"Power and electrical equipment is still the main driver of construction delays. If a new site requires upgrading an existing transmission line, a brand-new transmission line, new generation brought to the grid, it can impact timelines drastically."* He frames it qualitatively because he has no quantified ISO queue layer. PRD-PDB is exactly that layer.

His Weekly Take podcast Ep32 (Aug 2025) with Mortenson talked through 24/36/48-month delivery curves — but with no per-market quantitative attribution. PRD-PDB turns that podcast narrative into a number per market per week.

His Sun Belt I-20 corridor thesis (Austin, Charlotte, Alabama, Mississippi) is precisely the band of markets where **queue depth divergence from NoVA is the differentiating story** — and where JLL is also looking. Gordon needs to publish that divergence with defensible numbers before JLL's new research lead does.

And his explicit gap statement on AI-vs-traditional classification — *"difficult to determine which facilities are specifically designated for AI versus traditional storage or compute"* — is covered by DC Hub's `discovered_facilities.derived_use` field, layered into each brief.

## Why JLL Can't Replicate It Within 12-24 Months

JLL would need three things simultaneously: (1) a live ISO queue scraping infrastructure across 22 grids with state-level FERC/utility filing ingestion, (2) a 2,032-deal M&A overlay with shell-co naming pattern recognition tied to hyperscaler hierarchies, and (3) the 31-tool MCP layer to deliver any of it into an agentic broker workflow. DCHawk has none of these and is owned by Leeds Equity — a PE roll-up that historically underinvests in R&D. DC Byte has analyst-paced "continuous validation" but no live grid feeds. JLL would have to either acquire one of them (12+ months of M&A and integration) or build from scratch (2+ years to reach data parity, longer to reach the deal-corpus pattern recognition that takes years of accumulated transactions to train).

The deal corpus is the irreplicable asset. 2,032 verified transactions with grid-reality overlay cannot be backfilled — it had to happen contemporaneously.

## Implementation Cost for DC Hub

- **Build effort:** ~3 engineer-weeks. The underlying primitives all exist: `/api/v1/interconnection-queue/snapshot`, grid intelligence layer, `discovered_facilities` derived_use, M&A `list_transactions`, news ingestion. The PRD-PDB is an aggregator + a templated weekly PDF render + a private MCP route gated to a CBRE-issued enterprise key.
- **Ongoing maintenance:** ~4 hours/week of analyst-review of the auto-generated briefs before they ship Friday (must catch hallucinations — Gordon will not tolerate a single wrong number, given his Series 6/7/63/65 compliance posture).
- **Marginal infra cost:** negligible — same data, new render path.

## Pricing Structure

- **CRAWL (months 0-3, free):** PRD-PDB delivered weekly to Gordon and 2 named CBRE analysts. No charge. Builds dependency.
- **WALK (months 3-9, $50K/yr):** Expand to 10 named CBRE seats, add Tuesday deal pre-mortem alerts, MCP endpoint live. Priced as a research-tools line item, not a partnership — fits CBRE's procurement category without triggering corporate review.
- **RUN (months 9+, $150-250K/yr):** Enterprise seat for full CBRE Data Center Research team (~30 analysts globally) plus custom market additions on Gordon's request. Renewable annually. Methodology-footnote-only attribution preserved throughout.

## Risk: What Could Make CBRE Say No

The single biggest risk is **Gordon's compliance instinct rejecting the AI-vs-traditional classification methodology**. The `derived_use` field is heuristic, not audited. If Gordon's team can't reproduce the classification logic from a published methodology PDF, he won't cite it in H2 2026 even in a footnote. **Mitigation: ship a 4-page methodology PDF before the first brief drops, documenting classifier features, confidence intervals, and known false-positive rates.** Without this, the moat closes itself.

Secondary risk: Pat Lynch's brokerage side discovering the feed and demanding the same product but tuned to transactions rather than research. **Mitigation: design PRD-PDB v2 in parallel for Pat's pipeline.** That doubles the moat without dividing it.

## Concrete Crawl/Walk/Run for This Moat

**CRAWL (June-August 2026, pre-H2 publication window):**
- Week 1: Ship 4-page methodology PDF to Gordon, pre-cleared with his compliance instinct.
- Week 2-3: Build PRD-PDB v0 covering Gordon's 12 named primary markets from H2 2025.
- Week 4: First Friday brief drops. Free. Gordon's distribution list only.
- Weeks 5-12: Iterate on Gordon's feedback. Add Sun Belt I-20 markets (Austin, Charlotte, Alabama corridor). Goal: at least 3 PRD-PDB-sourced data points appear in Gordon's H2 2026 Trends report with DC Hub methodology footnote.

**WALK (September 2026 - March 2027, post-H2 launch):**
- Tuesday deal pre-mortem alerts go live for queue-position movements matching shell-co patterns.
- MCP endpoint opens to Gordon's 10 named analysts.
- $50K annual contract booked under CBRE Research's tooling line.
- Goal: Gordon cites DC Hub PRD-PDB methodology in his March 2027 H1 Trends and the 2027 US RE Market Outlook DC chapter. **Two consecutive cycles of citation = lock-in.**

**RUN (April 2027+):**
- Enterprise seat across CBRE DCS Research (Americas, EMEA, APAC).
- Annual renewal at $150-250K with custom market expansion (e.g., Quebec hydro corridor, offshore wind PPA tracking — both Gordon-named priorities).
- Pat Lynch's brokerage variant ships in parallel: same data, different render — pre-RFP capacity briefs for active CBRE pursuits.
- The methodology footnote becomes the moat: every CBRE H1/H2 report for two years has cited DC Hub, JLL/Cushman/Newmark have no comparable data layer, and switching costs (analyst muscle memory, deck templates, methodology citations) make displacement implausible.

**The win condition:** by H2 2027, "according to DC Hub's Power Delivery data" appears in CBRE Research with the same naturalness "according to CoStar" appears in CBRE office leasing reports today.
