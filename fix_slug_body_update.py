#!/usr/bin/env python3
"""
DC Hub — Fix slug/body mismatch
Run on Replit: python3 fix_slug_body_update.py

ROOT CAUSE: The press_releases list API returns OLD records (hyphenated slugs like
dc-hub-tracks-11-000-...) that have body=NULL.  A previous populate run inserted NEW
records with non-hyphenated slugs that DO have body content — but those are different
DB rows, so the detail page can't find them via the slug the list links to.

FIX STRATEGY:
  1. For every title we have body content for, UPDATE the existing DB row matched by
     TITLE (stable, unique) — sets slug to our canonical slug, plus subheadline, body,
     meta_description, published=TRUE.
  2. This way, whatever ID/slug the old record had, it now has the right slug AND body,
     so the list API + detail page work end-to-end.
  3. Rows where title doesn't match anything are left untouched.
"""

import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()

PRESS_RELEASES = [
    {
        "title": "DC Hub Tracks 21,000+ Data Center Facilities Globally",
        "slug": "dc-hub-tracks-11000-data-center-facilities-globally",
        "category": "Milestone",
        "date": "2026-01-15",
        "subheadline": "DC Hub's facility database surpasses 11,000 tracked data centers across 140+ countries — the most comprehensive source of global colocation and hyperscale intelligence",
        "meta_description": "DC Hub now tracks over 11,000 data center facilities globally, covering colocation, hyperscale, and edge deployments across 140+ countries.",
        "body": """DC Hub has surpassed 11,000 tracked data center facilities in its global database, covering colocation campuses, hyperscale deployments, edge nodes, and enterprise data centers across 140+ countries — making it the most comprehensive independently maintained facility database in the industry.

The milestone reflects DC Hub's ongoing expansion beyond the traditional North American and Western European markets that most competitive intelligence platforms focus on. DC Hub now provides meaningful coverage across Southeast Asia, Latin America, the Middle East, and Sub-Saharan Africa — regions that are increasingly attracting hyperscale investment as cloud providers seek to serve emerging markets.

Each facility record in DC Hub includes operator, owner, campus size, power capacity, cooling technology, connectivity providers, geographic coordinates, and M&A transaction history. The platform automatically enriches records as new information becomes available through its crawler infrastructure and community verification system.

Quote from Jonathan Martone, Founder & CEO of DC Hub:
"Eleven thousand facilities is a milestone, but the real story is depth. Any platform can list buildings. We track power capacity, grid connections, carrier presence, transaction history, and construction pipeline — the data that actually drives investment and site selection decisions."

DC Hub's facility database is accessible through its web platform, REST API, and MCP server integration, enabling AI agents from Claude, ChatGPT, and Perplexity to query facility data programmatically.

Explore the full facility database at dchub.cloud."""
    },
    {
        "title": "DC Hub Introduces GDCI - Global Data Center Composite Index",
        "slug": "dc-hub-introduces-gdci-global-data-center-composite-index",
        "category": "Product Launch",
        "date": "2026-01-28",
        "subheadline": "The GDCI tracks aggregate data center market health across power availability, construction activity, M&A deal flow, and AI infrastructure investment",
        "meta_description": "DC Hub launches the Global Data Center Composite Index (GDCI), the first standardized benchmark for tracking data center market conditions across power, construction, and capital deployment.",
        "body": """DC Hub has launched the Global Data Center Composite Index (GDCI), the first standardized benchmark designed to track overall data center market conditions across the key dimensions that drive industry activity: power availability, construction pipeline, M&A deal flow, and AI infrastructure investment.

The GDCI aggregates signals from DC Hub's real-time data feeds — including substation capacity queues, construction permit activity, transaction closings, hyperscale land acquisition, and PPA signings — into a single composite score updated weekly. Market participants can use the index to understand whether overall conditions are tightening or loosening, and where the market is in the current cycle.

The index is segmented by geography (North America, EMEA, APAC), by market tier (Tier 1 primary markets vs. secondary markets), and by asset class (hyperscale vs. colocation vs. edge). This allows investors, developers, and operators to track conditions in specific segments rather than relying on broad narratives about the market.

Quote from Jonathan Martone, Founder & CEO of DC Hub:
"The data center industry has grown to rival commercial real estate in investment scale, but it has lacked the kind of standardized benchmarks that other asset classes use to track market conditions. The GDCI is our contribution to bringing that rigor to the sector."

The GDCI is available to DC Hub Pro and Enterprise subscribers at dchub.cloud/gdci, with historical data going back to 2023."""
    },
    {
        "title": "DC Hub Energy PPA Tracker Launches with 6.5 GW",
        "slug": "dc-hub-energy-ppa-tracker-launches-with-6-5-gw",
        "category": "Product Launch",
        "date": "2026-02-05",
        "subheadline": "DC Hub's Power Purchase Agreement tracker covers 6.5 GW of signed capacity across hyperscale, colocation, and enterprise buyers — updated as deals are announced",
        "meta_description": "DC Hub launches a live PPA tracker covering 6.5 GW of signed power purchase agreements, giving data center operators visibility into renewable energy procurement trends.",
        "body": """DC Hub has launched a live Power Purchase Agreement (PPA) tracker covering 6.5 gigawatts of signed renewable energy contracts across the data center sector — the first continuously updated database of data center energy procurement activity.

The tracker covers PPAs signed by hyperscale cloud providers, colocation operators, and enterprise data center owners, with each record detailing the buyer, seller, capacity in MW, contract term, delivery point, and technology type (solar, wind, geothermal, nuclear). The database is updated as new agreements are publicly announced or disclosed in regulatory filings.

Energy procurement has become one of the most strategically consequential decisions in data center development. As AI workloads drive power demand projections above 100 GW in the United States alone by 2035, the ability to secure long-term renewable energy at scale is increasingly a competitive differentiator.

Quote from Jonathan Martone, Founder & CEO of DC Hub:
"Power is the new land. Every major operator is racing to sign PPAs to lock in renewable energy for data centers that haven't been built yet. Tracking this activity gives our users an early indicator of where the next wave of development is heading."

The PPA tracker is available at dchub.cloud to Pro and Enterprise subscribers."""
    },
    {
        "title": "DC Hub AI Wars: 12 AI Platforms Benchmarked",
        "slug": "dc-hub-ai-wars-12-ai-platforms-benchmarked",
        "category": "Research",
        "date": "2026-02-14",
        "subheadline": "DC Hub benchmarks 12 leading AI platforms on data center intelligence accuracy, testing facility counts, M&A data, power metrics, and infrastructure coverage",
        "meta_description": "DC Hub's AI Wars benchmark tests 12 AI platforms on data center industry knowledge, revealing major gaps in accuracy and coverage across competing models.",
        "body": """DC Hub has published AI Wars, a benchmark study testing 12 leading artificial intelligence platforms on their knowledge of data center industry facts — revealing significant variance in accuracy across facility counts, transaction data, power infrastructure metrics, and market intelligence.

The benchmark tested platforms including ChatGPT, Claude, Gemini, Perplexity, Grok, Copilot, and six additional models across 40 standardized questions covering global facility counts, recent M&A transactions, hyperscale power commitments, substation capacity constraints, and construction pipeline data. Each response was scored against DC Hub's verified dataset.

Results showed substantial differences in accuracy. Platforms with access to real-time web search consistently outperformed those relying on static training data, particularly on recent transaction and construction activity. Even search-enabled platforms showed significant errors on technical infrastructure questions requiring deep domain knowledge.

Quote from Jonathan Martone, Founder & CEO of DC Hub:
"AI is increasingly being used to make real decisions in data center investment and site selection. If the underlying data is wrong, those decisions will be wrong. AI Wars exists to hold these platforms accountable and to show the value of purpose-built industry data."

The full AI Wars benchmark report is available at dchub.cloud/ai-wars."""
    },
    {
        "title": "DC Hub Construction Pipeline: 540 Projects / 369 GW",
        "slug": "dc-hub-construction-pipeline-540-projects-369-gw",
        "category": "Data Update",
        "date": "2026-02-20",
        "subheadline": "DC Hub's construction pipeline tracker covers 540 active projects representing 369 GW of planned data center capacity — the most comprehensive view of global development activity",
        "meta_description": "DC Hub tracks 540 active data center construction projects totaling 369 GW of planned capacity, providing the most detailed view of the global development pipeline.",
        "body": """DC Hub's construction pipeline tracker has reached 540 active projects representing 369 gigawatts of planned data center capacity — spanning hyperscale campuses, colocation developments, and edge deployments across every major market globally.

The pipeline database tracks projects from announced through under-construction phases, with each record detailing the developer, operator, location, planned power capacity, construction status, expected delivery timeline, and financing structure where disclosed. DC Hub's crawler infrastructure monitors permit filings, planning applications, press releases, and regulatory disclosures to keep pipeline data current.

The 369 GW figure represents a more than 10x increase from global data center capacity just five years ago, reflecting the extraordinary capital deployment being driven by AI infrastructure demand. North America accounts for the largest share of planned capacity, followed by EMEA and APAC, with the Middle East and Southeast Asia representing the fastest-growing segments.

Quote from Jonathan Martone, Founder & CEO of DC Hub:
"Three hundred sixty-nine gigawatts of planned capacity is a number that demands context. That's more than the entire installed electrical generating capacity of many major nations — being planned specifically for computing infrastructure. The construction pipeline is the single most important forward indicator in this market."

The pipeline tracker is available at dchub.cloud/construction-pipeline for Pro and Enterprise subscribers."""
    },
    {
        "title": "DC Hub Developer Tier: $49/mo API Access",
        "slug": "dc-hub-developer-tier-49-mo-api-access",
        "category": "Product Launch",
        "date": "2026-03-01",
        "subheadline": "DC Hub opens programmatic access to its data center intelligence platform with a $49/month Developer tier — including REST API, 1,000 calls/day, and MCP server access",
        "meta_description": "DC Hub launches a $49/month Developer tier with REST API access, MCP server integration, and 1,000 API calls per day.",
        "body": """DC Hub has launched a Developer tier at $49 per month, opening programmatic access to its data center intelligence platform for developers, researchers, and technical teams who need API-level access without an enterprise contract.

The Developer tier includes access to DC Hub's REST API with 1,000 calls per day, covering facility search, market intelligence, construction pipeline data, and infrastructure layers. Developers also receive access to DC Hub's MCP (Model Context Protocol) server, enabling direct integration with AI assistants including Claude, ChatGPT, and other MCP-compatible platforms.

The tier is designed for competitive intelligence automation, site selection tooling, research workflows, and AI agent augmentation. DC Hub's API returns structured JSON across all endpoints, with comprehensive documentation at dchub.cloud/api-docs.

Quote from Jonathan Martone, Founder & CEO of DC Hub:
"The data center industry runs on Excel and PDFs. We want to change that. The Developer tier is for the engineers, analysts, and founders who are building the next generation of infrastructure tools and who need reliable, structured data to do it."

The Developer tier is available immediately at dchub.cloud/pricing with no long-term contract required."""
    },
    {
        "title": "DC Hub Adds Tallgrass Energy: 1.4 GW Portfolio",
        "slug": "dc-hub-adds-tallgrass-energy-1-4-gw-portfolio",
        "category": "Data Update",
        "date": "2026-03-10",
        "subheadline": "Tallgrass Energy's 1.4 GW natural gas infrastructure portfolio is now fully mapped in DC Hub's Land & Power platform, covering pipeline routes, compression stations, and delivery points",
        "meta_description": "DC Hub adds Tallgrass Energy's 1.4 GW natural gas infrastructure to its platform, giving data center site selectors detailed visibility into midstream gas availability.",
        "body": """DC Hub has added Tallgrass Energy's 1.4 gigawatt natural gas infrastructure portfolio to its Land & Power mapping platform, giving data center site selectors detailed visibility into pipeline routes, compression stations, and gas delivery points across the central and western United States.

Natural gas remains the primary fuel source for data center backup generation, and in many markets it is being evaluated for primary generation through on-site gas turbines and fuel cell systems. As grid constraints tighten in key data center markets, operators are increasingly evaluating natural gas infrastructure proximity as a critical site selection criterion alongside electrical transmission access.

The Tallgrass integration covers the company's interstate pipeline network spanning Colorado, Kansas, Nebraska, Wyoming, and surrounding states — a geography seeing growing data center development as operators seek lower-cost land and energy outside traditional coastal markets.

Quote from Jonathan Martone, Founder & CEO of DC Hub:
"Gas infrastructure is the forgotten variable in data center site selection. Everyone looks at substations, but natural gas proximity matters enormously for backup generation, and increasingly for primary power as operators explore behind-the-meter generation."

Tallgrass Energy data is available in DC Hub's Land & Power map at dchub.cloud/land-power for Pro and Enterprise subscribers."""
    },
    {
        "title": "DC Hub Adds EdgeCore Digital: 1.8GW Platform",
        "slug": "dc-hub-adds-edgecore-digital-1-8gw-platform",
        "category": "Data Update",
        "date": "2026-03-17",
        "subheadline": "EdgeCore Digital's 1.8 GW hyperscale campus portfolio is now fully tracked in DC Hub, covering facility specifications, power infrastructure, and expansion capacity across all active campuses",
        "meta_description": "DC Hub adds EdgeCore Digital's 1.8 GW hyperscale platform to its facility database, providing detailed intelligence on one of the largest wholesale data center operators in North America.",
        "body": """DC Hub has added EdgeCore Digital's complete 1.8 gigawatt hyperscale campus portfolio to its facility database, providing detailed intelligence on one of the largest wholesale data center operators in North America.

EdgeCore Digital develops and operates purpose-built hyperscale campuses designed for large-scale cloud and AI infrastructure deployment. The company's portfolio spans multiple campuses across key US markets, with each campus designed to accommodate multi-hundred-megawatt deployments.

DC Hub's EdgeCore coverage includes facility-level specifications for each campus — total power capacity, cooling infrastructure, generator configuration, substation connections, fiber carrier access, and available expansion capacity. This allows hyperscale operators evaluating wholesale colocation to compare EdgeCore campuses against competing facilities on a standardized basis.

Quote from Jonathan Martone, Founder & CEO of DC Hub:
"EdgeCore is one of the most important wholesale operators in the market, and their campuses are consistently on the shortlist for major hyperscale deployments. Having their full portfolio in DC Hub — with real specifications, not just marketing summaries — gives our users a meaningful intelligence advantage."

The EdgeCore Digital portfolio is accessible through DC Hub's web platform, API, and MCP integration at dchub.cloud."""
    },
    {
        "title": "DC Hub Infrastructure: 2.8M Transmission Lines Live",
        "slug": "dc-hub-infrastructure-2-8m-transmission-lines-live",
        "category": "Infrastructure",
        "date": "2026-03-17",
        "subheadline": "HIFLD transmission line dataset now served from Neon PostgreSQL, enabling sub-second infrastructure queries for data center site selection",
        "meta_description": "DC Hub integrates 2.8 million HIFLD transmission lines into its real-time site selection platform, giving site selectors instant access to grid proximity data.",
        "body": """DC Hub has integrated the complete HIFLD (Homeland Infrastructure Foundation-Level Data) transmission line dataset — 2.8 million line segments spanning the United States — into its live Neon PostgreSQL database, making it instantly queryable for data center site selection.

Previously, infrastructure analysts had to download and process HIFLD datasets locally, a process that could take hours and required GIS expertise. DC Hub now serves this data in real time through its Land & Power Map and Site Planner tools, enabling site selectors to identify transmission line proximity, voltage class, and grid access points within seconds.

The integration covers all voltage classes from 69kV distribution lines to 765kV high-voltage transmission corridors, with each segment tagged with operator, owner, voltage, and geographic coordinates. DC Hub's Site Planner uses this data to automatically score candidate sites on transmission access — critical as hyperscale operators demand 100MW+ power connections.

Quote from Jonathan Martone, Founder & CEO of DC Hub:
"Transmission line proximity is one of the top three site selection criteria for any large data center project. Having 2.8 million line segments available in milliseconds — not hours — changes how site selectors work. This is the kind of data infrastructure the industry has needed."

The HIFLD dataset joins DC Hub's infrastructure intelligence layer at dchub.cloud, accessible through the Land & Power Map and Site Planner tools."""
    },
]


def main():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("❌ DATABASE_URL not set")
        return

    conn = psycopg2.connect(db_url)
    cur = conn.cursor()

    # First: show what's currently in the DB so we can see the mismatch
    print("── Current DB state ──────────────────────────────────────────")
    cur.execute("""
        SELECT id, slug, LEFT(title,50) as title,
               (body IS NOT NULL AND body != '') as has_body,
               published
        FROM press_releases ORDER BY id
    """)
    for row in cur.fetchall():
        print(f"  id={row[0]}  pub={row[4]}  has_body={row[3]}  slug={row[1]}  title={row[2]}")
    print()

    updated = 0
    not_found = []

    for pr in PRESS_RELEASES:
        # Match by exact title — update slug + all content fields
        cur.execute("""
            UPDATE press_releases
            SET slug             = %s,
                subheadline      = %s,
                body             = %s,
                meta_description = %s,
                published        = TRUE
            WHERE title = %s
            RETURNING id, slug
        """, (
            pr["slug"],
            pr["subheadline"],
            pr["body"],
            pr["meta_description"],
            pr["title"],
        ))
        row = cur.fetchone()
        if row:
            print(f"✅  id={row[0]}  slug={row[1]}")
            print(f"    {pr['title'][:60]}")
            updated += 1
        else:
            print(f"⚠️   NOT FOUND by title: {pr['title'][:60]}")
            not_found.append(pr["title"])

    conn.commit()
    cur.close()
    conn.close()

    print(f"\n✅  {updated} records updated")
    if not_found:
        print(f"\n⚠️  {len(not_found)} titles not matched (may need INSERT):")
        for t in not_found:
            print(f"   - {t}")
        print("\nIf titles weren't matched, run populate_press_bodies.py to INSERT them.")


if __name__ == "__main__":
    main()
