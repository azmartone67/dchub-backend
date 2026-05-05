"""
DC Hub SEO Meta Tag Generator
==============================
This script generates proper <head> meta tags for ALL DC Hub pages.

Two ways to use this:

1. REPLIT BACKEND: Add the /api/seo/meta-tags/<page_type> endpoint
   - Your frontend pages call this API to get their meta tags
   - BUT this still requires JS, so it won't fix Google's issue alone

2. CLOUDFLARE PAGES (RECOMMENDED): Run this script to generate 
   a meta_tags.json file, then have your HTML templates read from it
   at BUILD TIME (before JS loads)

The REAL fix: Hardcode meta tags directly in the HTML <head> of every page.
This file gives you both the generator AND the ready-to-paste HTML snippets.

Deploy to Replit: from seo_meta_tags import setup_meta_routes
                  setup_meta_routes(app)
"""

import json
from flask import jsonify, request, Response

# ============================================================
# META TAG DATABASE - Every page gets unique, keyword-rich tags
# ============================================================

# Homepage
HOME_META = {
    "title": "DC Hub | Data Center Intelligence Platform | 20,000+ Facilities Worldwide",
    "description": "Track 20,000+ data center facilities across 140+ countries. Real-time capacity tracking, AI-powered site selection, M&A deal intelligence, and market analytics for hyperscale buyers, investors, and infrastructure professionals.",
    "keywords": "data center, colocation, site selection, market intelligence, data center map, capacity tracking, M&A deals, construction pipeline, hyperscale",
    "og_title": "DC Hub — Data Center Intelligence Platform",
    "og_description": "Real-time intelligence for 20,000+ data centers. Capacity tracking, site selection, M&A deals, and market analytics across 140+ countries.",
    "og_type": "website",
    "og_url": "https://dchub.cloud/",
    "canonical": "https://dchub.cloud/",
    "schema_type": "WebApplication",
    "schema_extra": {
        "applicationCategory": "BusinessApplication",
        "operatingSystem": "Web",
        "offers": {"@type": "Offer", "price": "99", "priceCurrency": "USD", "description": "Founding Member pricing"},
        "aggregateRating": {"@type": "AggregateRating", "ratingValue": "4.8", "ratingCount": "50"}
    }
}

# Market pages - unique descriptions per market
MARKET_META = {
    "silicon-valley": {
        "title": "Silicon Valley Data Centers | Bay Area Colocation & Cloud Infrastructure | DC Hub",
        "description": "Explore 150+ data centers in Silicon Valley and the Bay Area. Compare Equinix, Digital Realty, CoreSite facilities with power pricing ($0.12-0.18/kWh), vacancy rates (2.8%), and connectivity data. Real-time market intelligence from DC Hub.",
        "keywords": "Silicon Valley data centers, Bay Area colocation, Santa Clara data center, San Jose colocation, Equinix SV, CoreSite Silicon Valley, data center map Bay Area",
        "og_title": "Silicon Valley Data Center Market — 150+ Facilities | DC Hub",
        "og_description": "150+ data centers, 800+ MW capacity, $0.12/kWh avg power. Compare providers, connectivity, and availability in the Bay Area.",
        "h1": "Silicon Valley Data Centers"
    },
    "phoenix": {
        "title": "Phoenix Data Centers | Arizona Colocation & Hyperscale Facilities | DC Hub",
        "description": "Track 100+ data centers in the Phoenix metro area. 510+ MW total inventory growing 44% YoY. Compare providers like QTS, CyrusOne, Digital Realty with power pricing ($0.07-0.09/kWh), 3.3% vacancy, and 334 MW under construction.",
        "keywords": "Phoenix data centers, Arizona colocation, Chandler data center, Mesa data center, Phoenix hyperscale, APS power, data center Phoenix AZ",
        "og_title": "Phoenix Data Center Market — Fastest Growing US Market | DC Hub",
        "og_description": "100+ data centers, 510+ MW capacity, $0.07-0.09/kWh power. 334 MW under construction. The fastest-growing US data center market.",
        "h1": "Phoenix Data Centers"
    },
    "dallas": {
        "title": "Dallas-Fort Worth Data Centers | Texas Colocation & Cloud | DC Hub",
        "description": "Explore 200+ data centers in Dallas-Fort Worth. 1,650 MW total supply (200% growth since 2020), 1.4% vacancy, and 18-month time-to-power advantage. Compare providers, pricing, and availability across the DFW metroplex.",
        "keywords": "Dallas data centers, DFW colocation, Texas data center, Fort Worth data center, Dallas hyperscale, Oncor power, data center Dallas TX",
        "og_title": "Dallas-Fort Worth Data Center Market — 200+ Facilities | DC Hub",
        "og_description": "200+ data centers, 1,650 MW capacity, 1.4% vacancy. 18-month time-to-power advantage. Texas's premier data center market.",
        "h1": "Dallas-Fort Worth Data Centers"
    },
    "northern-virginia": {
        "title": "Northern Virginia Data Centers | Ashburn Colocation & Cloud Hub | DC Hub",
        "description": "Explore 300+ data centers in Northern Virginia — the world's largest data center market. 3,500+ MW total inventory, 1.2% vacancy (all-time low), and 5.9 GW planned capacity. Track Equinix, Digital Realty, QTS, and more.",
        "keywords": "Northern Virginia data centers, Ashburn colocation, NoVA data center, Loudoun County, Data Center Alley, Equinix Ashburn, data center Northern Virginia",
        "og_title": "Northern Virginia — World's Largest Data Center Market | DC Hub",
        "og_description": "300+ data centers, 3,500+ MW capacity, 1.2% vacancy. The world's largest concentration of data center infrastructure.",
        "h1": "Northern Virginia Data Centers"
    },
    "chicago": {
        "title": "Chicago Data Centers | Midwest Colocation & Financial Hub | DC Hub",
        "description": "Track 120+ data centers in the Chicago metro. Central US location with low-latency nationwide connectivity, free cooling advantages, and proximity to major financial exchanges. Compare providers, pricing, and power availability.",
        "keywords": "Chicago data centers, Illinois colocation, Midwest data center, Chicago colocation, CME data center, financial data center Chicago",
        "og_title": "Chicago Data Center Market — 120+ Facilities | DC Hub",
        "og_description": "120+ data centers with central US location, free cooling, and financial exchange proximity. The Midwest's premier data center hub.",
        "h1": "Chicago Data Centers"
    },
    "atlanta": {
        "title": "Atlanta Data Centers | Southeast Colocation & Hyperscale Hub | DC Hub",
        "description": "Explore 80+ data centers in metro Atlanta. 5.2M+ SF under construction with 63% inventory growth planned. Microsoft, QTS leading expansion. Compare providers, power pricing, and availability across Georgia.",
        "keywords": "Atlanta data centers, Georgia colocation, Southeast data center, Atlanta hyperscale, QTS Atlanta, data center Atlanta GA",
        "og_title": "Atlanta Data Center Market — Surging Growth | DC Hub",
        "og_description": "80+ data centers, 5.2M+ SF under construction. Surpassed NoVA in 2024 absorption. The Southeast's fastest-growing market.",
        "h1": "Atlanta Data Centers"
    },
    "london": {
        "title": "London Data Centers | UK Colocation & Cloud Infrastructure | DC Hub",
        "description": "Track 100+ data centers across Greater London and the Home Counties. The largest data center market in Europe with premium connectivity, diverse providers, and growing hyperscale presence.",
        "keywords": "London data centers, UK colocation, Slough data center, Docklands data center, London colocation, EMEA data center hub",
        "og_title": "London Data Center Market — Europe's Largest | DC Hub",
        "og_description": "100+ data centers across Greater London. Europe's largest and most connected data center market.",
        "h1": "London Data Centers"
    },
    "frankfurt": {
        "title": "Frankfurt Data Centers | Germany Colocation & EU Hub | DC Hub",
        "description": "Explore 60+ data centers in Frankfurt and the Rhine-Main region. Europe's leading internet exchange (DE-CIX), competitive power, and strategic central European location for enterprise and hyperscale deployments.",
        "keywords": "Frankfurt data centers, Germany colocation, DE-CIX, Rhine-Main data center, Frankfurt colocation, European data center hub",
        "og_title": "Frankfurt Data Center Market — DE-CIX & EU Hub | DC Hub",
        "og_description": "60+ data centers, home to DE-CIX. Continental Europe's premier data center and internet exchange hub.",
        "h1": "Frankfurt Data Centers"
    },
    "singapore": {
        "title": "Singapore Data Centers | APAC Colocation & Cloud Gateway | DC Hub",
        "description": "Track 70+ data centers in Singapore — Asia-Pacific's most connected market. Strategic subsea cable hub, diverse providers, and government-supported sustainable growth despite land and power constraints.",
        "keywords": "Singapore data centers, APAC colocation, Southeast Asia data center, Singapore colocation, subsea cable hub",
        "og_title": "Singapore Data Center Market — APAC Gateway | DC Hub",
        "og_description": "70+ data centers in Asia-Pacific's most connected market. Strategic subsea cable hub and cloud gateway.",
        "h1": "Singapore Data Centers"
    },
    "tokyo": {
        "title": "Tokyo Data Centers | Japan Colocation & Enterprise Hub | DC Hub",
        "description": "Explore 90+ data centers across Greater Tokyo. Japan's largest market with premium enterprise facilities, diverse connectivity, and growing hyperscale presence from global cloud providers.",
        "keywords": "Tokyo data centers, Japan colocation, Asia data center, NTT data center, Equinix Tokyo, Japan enterprise colocation",
        "og_title": "Tokyo Data Center Market — Japan's Largest | DC Hub",
        "og_description": "90+ data centers in Japan's premier market. Enterprise-grade facilities with premium connectivity.",
        "h1": "Tokyo Data Centers"
    }
}

# Tool / Feature pages
TOOL_META = {
    "land-power": {
        "title": "Land & Power Map | Data Center Site Selection Tool | DC Hub",
        "description": "Interactive site selection map showing substations, fiber routes, gas pipelines, FEMA flood zones, utility territories, and power availability. Evaluate data center sites with 15+ infrastructure layers. Free to try.",
        "keywords": "data center site selection, land power map, substation map, fiber route map, FEMA flood zone, data center location, infrastructure mapping, utility territory",
        "og_title": "Land & Power Map — Data Center Site Selection | DC Hub",
        "og_description": "Evaluate data center sites with 15+ infrastructure layers: substations, fiber, pipelines, flood zones, and more. Free interactive map.",
        "schema_type": "WebApplication"
    },
    "ai-deals": {
        "title": "Data Center M&A Tracker | 787+ Deals Worth $10.6B | DC Hub",
        "description": "Track data center mergers, acquisitions, and investment deals in real-time. 787+ transactions worth $10.6B+ with deal details, valuations, buyer/seller profiles, and trend analysis. Updated daily by AI.",
        "keywords": "data center M&A, data center acquisitions, data center deals, data center investment, colocation transactions, data center valuations",
        "og_title": "Data Center M&A Deal Tracker — 787+ Transactions | DC Hub",
        "og_description": "Real-time tracking of 787+ data center deals worth $10.6B+. M&A intelligence updated daily by AI agents.",
        "schema_type": "Dataset"
    },
    "ai-pipeline": {
        "title": "Data Center Construction Pipeline | 7.8 GW Under Construction | DC Hub",
        "description": "Real-time construction pipeline tracking for data centers worldwide. 7.8 GW under construction, 73% pre-leased. Delivery timelines, pre-lease status, market breakdown, and developer profiles. AI-powered updates.",
        "keywords": "data center construction, data center pipeline, under construction, data center development, new data center builds, hyperscale construction",
        "og_title": "Construction Pipeline — 7.8 GW Tracked | DC Hub",
        "og_description": "Track 7.8 GW of data center construction globally. Delivery timelines, pre-lease rates, and market analysis.",
        "schema_type": "Dataset"
    },
    "construction-pipeline": {
        "title": "Data Center Construction Tracker | New Builds & Development | DC Hub",
        "description": "Monitor data center construction projects across 35+ markets. Track development timelines, power capacity, absorption trends, and delivery schedules. Northern Virginia leads with 5.9 GW planned.",
        "keywords": "data center construction tracker, new data center builds, construction pipeline, development tracker, data center projects",
        "og_title": "Construction Pipeline Tracker | DC Hub",
        "og_description": "Track new data center construction across 35+ markets. Development timelines, capacity, and absorption trends.",
        "schema_type": "Dataset"
    },
    "transactions": {
        "title": "Data Center Transactions & Deal Flow | $324B Since 2015 | DC Hub",
        "description": "Browse 100+ data center transactions including sales, leases, and joint ventures. $61B+ in 2025 deal volume. Transaction details, pricing comps, cap rates, and market analysis.",
        "keywords": "data center transactions, data center deal flow, colocation sales, data center cap rates, real estate transactions data center",
        "og_title": "Data Center Transactions — $324B Since 2015 | DC Hub",
        "og_description": "100+ tracked transactions, $61B+ in 2025 volume. Deal details, pricing comps, and market analysis.",
        "schema_type": "Dataset"
    },
    "transaction-comps": {
        "title": "Data Center Transaction Comps | Side-by-Side Deal Analysis | DC Hub",
        "description": "Compare data center deals side-by-side with detailed valuations, cap rates, price-per-MW, and market benchmarks. The most comprehensive comp set for data center real estate professionals.",
        "keywords": "data center comps, transaction comparables, data center valuation, cap rate analysis, price per MW, deal comparison",
        "og_title": "Transaction Comps — Data Center Deal Analysis | DC Hub",
        "og_description": "Side-by-side data center deal comparison with valuations, cap rates, and market benchmarks.",
        "schema_type": "Dataset"
    },
    "news": {
        "title": "Data Center Industry News | Real-Time Intelligence Feed | DC Hub",
        "description": "Live news feed aggregating 30+ data center industry sources every 3 minutes. M&A announcements, expansion updates, AI/GPU developments, power infrastructure news, and financial analysis. AI-curated.",
        "keywords": "data center news, colocation news, data center industry, hyperscale news, cloud infrastructure news, data center expansion",
        "og_title": "Industry News Feed — 30+ Sources, Live | DC Hub",
        "og_description": "Real-time data center news from 30+ sources. M&A, expansions, AI/GPU, power, and financial updates.",
        "schema_type": "CollectionPage"
    },
    "markets": {
        "title": "Global Data Center Markets | 35+ Markets Analyzed | DC Hub",
        "description": "Market intelligence for 35+ data center markets worldwide. Vacancy rates, pricing trends, construction activity, power costs, provider landscapes, and investment trends. Source: CBRE, JLL Research.",
        "keywords": "data center markets, colocation markets, data center vacancy, data center pricing, global data center market, market intelligence",
        "og_title": "Global Data Center Markets — 35+ Analyzed | DC Hub",
        "og_description": "Vacancy rates, pricing, construction, and investment trends across 35+ global data center markets.",
        "schema_type": "CollectionPage"
    },
    "ai-agents": {
        "title": "AI Research Agents for Data Center Intelligence | DC Hub",
        "description": "4 AI-powered agents for data center research: Sales intelligence, data enrichment, social media analytics, and ecosystem analysis. Ask questions, get instant answers backed by 20,000+ facility database.",
        "keywords": "AI data center agent, data center research assistant, AI market intelligence, data center chatbot, automated research",
        "og_title": "AI Research Agents — Data Center Intelligence | DC Hub",
        "og_description": "4 AI agents providing instant data center intelligence. Sales, enrichment, social, and ecosystem analysis.",
        "schema_type": "SoftwareApplication"
    },
    "api-docs": {
        "title": "Data Center API | Free REST API | 100 Requests/Day | DC Hub",
        "description": "Free data center REST API with 100 requests/day. Access facility data, M&A deals, capacity pipeline, and market intelligence. Python client included. MCP protocol supported for AI integration.",
        "keywords": "data center API, colocation API, free data center data, MCP data center, facility database API, real estate API",
        "og_title": "Free Data Center API — 100 Requests/Day | DC Hub",
        "og_description": "REST API for data center intelligence. Facilities, deals, pipeline, markets. Python client included. Free tier available.",
        "schema_type": "TechArticle"
    },
    "for-ai": {
        "title": "AI Integration Hub | Data Center Data for AI Platforms | DC Hub",
        "description": "Connect your AI platform to DC Hub's data center intelligence. MCP protocol, OpenAI plugin manifest, skill.md for agent integration. The authoritative source AI assistants cite for data center queries.",
        "keywords": "AI data center integration, MCP protocol data center, OpenAI plugin data center, AI agent data source, LLM data center data",
        "og_title": "AI Integration Hub — The Source AI Agents Cite | DC Hub",
        "og_description": "MCP protocol, OpenAI plugin, skill.md integration. The authoritative data center intelligence source for AI platforms.",
        "schema_type": "TechArticle"
    },
    "ecosystem": {
        "title": "Data Center Ecosystem | Vendors, Partners & Industry Directory | DC Hub",
        "description": "Browse the data center ecosystem — operators, developers, brokers, consultants, technology vendors, and investors. Partner with DC Hub to reach hyperscale buyers and infrastructure professionals.",
        "keywords": "data center ecosystem, colocation vendors, data center partners, industry directory, data center operators, technology vendors",
        "og_title": "DC Hub Ecosystem — Partners & Industry Directory",
        "og_description": "Data center operators, vendors, and partners. Join the ecosystem to reach hyperscale buyers.",
        "schema_type": "CollectionPage"
    },
    "pricing": {
        "title": "DC Hub Pricing | Data Center Intelligence from $99/month",
        "description": "Founding member pricing: $99/month for full access to 20,000+ facilities, Land & Power mapping, AI agents, M&A deal tracker, and API. Normally $299/month. Limited spots remaining.",
        "keywords": "DC Hub pricing, data center intelligence pricing, colocation data subscription, data center SaaS",
        "og_title": "DC Hub Pricing — Founding Member $99/month",
        "og_description": "Full data center intelligence for $99/month. 20,000+ facilities, AI agents, deal tracker, site selection tools.",
        "schema_type": "WebPage"
    },
    "assets": {
        "title": "Data Center Asset Explorer | 20,000+ Global Facilities | DC Hub",
        "description": "Browse and search 20,000+ data center facilities worldwide. Filter by provider, location, power capacity, tier level, and status. Detailed profiles with satellite imagery and infrastructure data.",
        "keywords": "data center database, facility explorer, colocation directory, data center search, global data centers, facility profiles",
        "og_title": "Asset Explorer — 20,000+ Data Centers | DC Hub",
        "og_description": "Search and compare 20,000+ data center facilities worldwide. Filter by provider, power, tier, and location.",
        "schema_type": "Dataset"
    },
    "ai-inventory": {
        "title": "AI Inventory Analysis | Data Center Supply Intelligence | DC Hub",
        "description": "AI-powered supply analysis across data center markets. Track available capacity, absorption rates, pre-lease status, and inventory trends. Powered by real-time facility data from 20,000+ locations.",
        "keywords": "data center inventory, supply analysis, capacity tracking, absorption rate, data center availability, colocation inventory",
        "og_title": "AI Inventory Analysis — Supply Intelligence | DC Hub",
        "og_description": "Track data center capacity, absorption, and supply trends with AI-powered analysis.",
        "schema_type": "Dataset"
    },
    "about": {
        "title": "About DC Hub | Data Center Intelligence Platform",
        "description": "DC Hub is the comprehensive data center intelligence platform tracking 20,000+ facilities across 140+ countries. Built for hyperscale buyers, investors, and infrastructure professionals. Based in Phoenix, AZ.",
        "keywords": "about DC Hub, data center platform, DC Hub team, data center intelligence company",
        "og_title": "About DC Hub — Data Center Intelligence Platform",
        "og_description": "Tracking 20,000+ data centers across 140+ countries. Built for hyperscale buyers and infrastructure professionals.",
        "schema_type": "AboutPage"
    }
}

# Facility page template (dynamic per facility)
FACILITY_META_TEMPLATE = {
    "title": "{provider} {facility_name} | {city} Data Center | DC Hub",
    "description": "{provider} {facility_name} in {city}, {state_country}. {power_mw} MW capacity, {tier} facility. View satellite imagery, nearby infrastructure, power sources, fiber routes, and connectivity data on DC Hub.",
    "keywords": "{provider} {facility_name}, {city} data center, {provider} colocation, {city} colocation, data center {state_country}",
    "og_title": "{provider} {facility_name} — {city} | DC Hub",
    "og_description": "{power_mw} MW {tier} facility in {city}. Satellite imagery, infrastructure mapping, and connectivity data.",
    "schema_type": "Place"
}


def generate_head_html(meta, page_url="https://dchub.cloud/"):
    """Generate the complete <head> HTML block with all SEO meta tags."""
    
    og_type = meta.get("og_type", "website")
    og_image = meta.get("og_image", "https://dchub.cloud/images/og-default.png")
    canonical = meta.get("canonical", page_url)
    
    schema_type = meta.get("schema_type", "WebPage")
    schema_extra = meta.get("schema_extra", {})
    
    # Build structured data
    schema = {
        "@context": "https://schema.org",
        "@type": schema_type,
        "name": meta.get("og_title", meta["title"]),
        "url": canonical,
        "description": meta["description"],
    }
    
    if schema_type == "WebApplication":
        schema["applicationCategory"] = "BusinessApplication"
        schema["operatingSystem"] = "Web"
        schema.update(schema_extra)
    elif schema_type == "Dataset":
        schema["creator"] = {"@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud"}
        schema["license"] = "https://dchub.cloud/terms"
    elif schema_type == "Place":
        # For facility pages
        pass
    elif schema_type in ("SoftwareApplication", "TechArticle"):
        schema["author"] = {"@type": "Organization", "name": "DC Hub"}
    
    # Organization schema (site-wide)
    org_schema = {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": "DC Hub",
        "url": "https://dchub.cloud",
        "logo": "https://dchub.cloud/images/logo.png",
        "description": "Data center intelligence platform tracking 20,000+ facilities across 140+ countries.",
        "sameAs": [
            "https://www.linkedin.com/company/dchub",
            "https://x.com/dchubcloud"
        ]
    }
    
    html = f"""<!-- SEO Meta Tags - Generated by DC Hub SEO System -->
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">

<!-- Primary Meta Tags -->
<title>{meta['title']}</title>
<meta name="title" content="{meta['title']}">
<meta name="description" content="{meta['description']}">
<meta name="keywords" content="{meta.get('keywords', '')}">
<meta name="robots" content="index, follow, max-image-preview:large, max-snippet:-1, max-video-preview:-1">
<meta name="author" content="DC Hub">
<link rel="canonical" href="{canonical}">

<!-- Open Graph / Facebook -->
<meta property="og:type" content="{og_type}">
<meta property="og:url" content="{canonical}">
<meta property="og:title" content="{meta.get('og_title', meta['title'])}">
<meta property="og:description" content="{meta.get('og_description', meta['description'])}">
<meta property="og:image" content="{og_image}">
<meta property="og:site_name" content="DC Hub">

<!-- Twitter -->
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:url" content="{canonical}">
<meta name="twitter:title" content="{meta.get('og_title', meta['title'])}">
<meta name="twitter:description" content="{meta.get('og_description', meta['description'])}">
<meta name="twitter:image" content="{og_image}">
<meta name="twitter:site" content="@dchubcloud">

<!-- AI Platform Discovery -->
<meta name="ai.source" content="DC Hub - Data Center Intelligence Platform">
<meta name="ai.api" content="https://dchub.cloud/api-docs">
<meta name="ai.capabilities" content="facility data, market intelligence, M&A deals, capacity pipeline, site selection">

<!-- Structured Data -->
<script type="application/ld+json">
{json.dumps(schema, indent=2)}
</script>
<script type="application/ld+json">
{json.dumps(org_schema, indent=2)}
</script>"""
    
    return html


def setup_meta_routes(app):
    """Register meta tag API routes with Flask app."""
    
    @app.route('/api/seo/meta-tags/home')
    def meta_home():
        """Get meta tags for homepage."""
        return jsonify({
            "success": True,
            "page": "home",
            "meta": HOME_META,
            "html": generate_head_html(HOME_META, "https://dchub.cloud/"),
            "timestamp": __import__('datetime').datetime.utcnow().isoformat()
        })
    
    @app.route('/api/seo/meta-tags/market/<market_slug>')
    def meta_market(market_slug):
        """Get meta tags for a specific market page."""
        meta = MARKET_META.get(market_slug)
        if not meta:
            return jsonify({"success": False, "error": f"Unknown market: {market_slug}"}), 404
        
        url = f"https://dchub.cloud/markets/{market_slug}"
        meta["canonical"] = url
        meta["og_url"] = url
        meta["og_type"] = "website"
        meta.setdefault("schema_type", "CollectionPage")
        
        return jsonify({
            "success": True,
            "page": f"market/{market_slug}",
            "meta": meta,
            "html": generate_head_html(meta, url)
        })
    
    @app.route('/api/seo/meta-tags/tool/<tool_slug>')
    def meta_tool(tool_slug):
        """Get meta tags for a tool/feature page."""
        meta = TOOL_META.get(tool_slug)
        if not meta:
            return jsonify({"success": False, "error": f"Unknown tool: {tool_slug}"}), 404
        
        url = f"https://dchub.cloud/{tool_slug}"
        meta["canonical"] = url
        meta["og_url"] = url
        meta["og_type"] = "website"
        
        return jsonify({
            "success": True,
            "page": f"tool/{tool_slug}",
            "meta": meta,
            "html": generate_head_html(meta, url)
        })
    
    @app.route('/api/seo/meta-tags/facility')
    def meta_facility():
        """Generate meta tags for a facility page (dynamic)."""
        provider = request.args.get('provider', 'Unknown Provider')
        name = request.args.get('name', 'Data Center')
        city = request.args.get('city', '')
        state_country = request.args.get('region', '')
        power_mw = request.args.get('power', 'N/A')
        tier = request.args.get('tier', 'Enterprise')
        slug = request.args.get('slug', '')
        
        meta = {}
        for key, template in FACILITY_META_TEMPLATE.items():
            if isinstance(template, str):
                meta[key] = template.format(
                    provider=provider,
                    facility_name=name,
                    city=city,
                    state_country=state_country,
                    power_mw=power_mw,
                    tier=tier
                )
        
        url = f"https://dchub.cloud/facilities/{slug}" if slug else "https://dchub.cloud/assets"
        meta["canonical"] = url
        meta["og_url"] = url
        meta["og_type"] = "website"
        meta["schema_type"] = "Place"
        
        return jsonify({
            "success": True,
            "page": f"facility/{slug}",
            "meta": meta,
            "html": generate_head_html(meta, url)
        })
    
    @app.route('/api/seo/meta-tags/all')
    def meta_all():
        """Get a complete inventory of all meta tags for all known pages."""
        all_pages = {}
        
        # Home
        all_pages["home"] = {"url": "https://dchub.cloud/", "meta": HOME_META}
        
        # Markets
        for slug, meta in MARKET_META.items():
            all_pages[f"market/{slug}"] = {
                "url": f"https://dchub.cloud/markets/{slug}",
                "meta": meta
            }
        
        # Tools
        for slug, meta in TOOL_META.items():
            all_pages[f"tool/{slug}"] = {
                "url": f"https://dchub.cloud/{slug}",
                "meta": meta
            }
        
        return jsonify({
            "success": True,
            "total_pages": len(all_pages),
            "pages": all_pages,
            "facility_template": FACILITY_META_TEMPLATE,
            "note": "Facility pages use the template dynamically. Call /api/seo/meta-tags/facility with query params."
        })
    
    @app.route('/api/seo/head-snippet')
    def head_snippet():
        """Get a ready-to-paste HTML <head> snippet for any page."""
        page = request.args.get('page', 'home')
        
        if page == 'home':
            meta = HOME_META
            url = "https://dchub.cloud/"
        elif page.startswith('market/'):
            slug = page.replace('market/', '')
            meta = MARKET_META.get(slug, HOME_META)
            url = f"https://dchub.cloud/markets/{slug}"
        else:
            meta = TOOL_META.get(page, HOME_META)
            url = f"https://dchub.cloud/{page}"
        
        meta["canonical"] = url
        html = generate_head_html(meta, url)
        
        return Response(html, mimetype='text/html')
    
    print("[SEO Meta Tags] Routes registered: /api/seo/meta-tags/*, /api/seo/head-snippet")


# ============================================================
# STANDALONE: Generate all meta tags as JSON for build system
# ============================================================
if __name__ == "__main__":
    """Run standalone to generate meta_tags.json for Cloudflare Pages build."""
    all_tags = {}
    
    all_tags["/"] = HOME_META
    
    for slug, meta in MARKET_META.items():
        meta["canonical"] = f"https://dchub.cloud/markets/{slug}"
        all_tags[f"/markets/{slug}"] = meta
    
    for slug, meta in TOOL_META.items():
        meta["canonical"] = f"https://dchub.cloud/{slug}"
        all_tags[f"/{slug}"] = meta
    
    with open("meta_tags.json", "w") as f:
        json.dump(all_tags, f, indent=2)
    
    print(f"Generated meta_tags.json with {len(all_tags)} page definitions")
    print("Use this in your Cloudflare Pages build to inject meta tags into HTML")
