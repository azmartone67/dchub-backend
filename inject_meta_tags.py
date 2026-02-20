#!/usr/bin/env python3
"""
DC Hub SEO Meta Tag Injector
==============================
Run this script BEFORE deploying to Cloudflare Pages.
It injects proper <meta> tags into the <head> of every HTML file.

Usage:
    python3 inject_meta_tags.py /path/to/your/cloudflare/build/

What it does:
1. Reads each .html file
2. Identifies the page type (home, market, tool, facility)
3. Injects the correct meta description, Open Graph, Twitter, and Schema.org tags
4. Writes the file back

This ensures Googlebot sees meta tags on the FIRST paint (no JS required).

WHY THIS MATTERS:
Google shows "We cannot provide a description for this page right now" because
your meta tags are either missing or set via JavaScript AFTER page load.
Googlebot's JS renderer is limited and often misses dynamically-set meta tags.
This script hardcodes them in the HTML so they're visible immediately.
"""

import os
import re
import sys
import json
from pathlib import Path

# ============================================================
# META TAG DATABASE
# ============================================================

HOME_META = {
    "title": "DC Hub | Data Center Intelligence Platform | 20,000+ Facilities Worldwide",
    "description": "Track 20,000+ data center facilities across 140+ countries. Real-time capacity tracking, AI-powered site selection, M&A deal intelligence, and market analytics for hyperscale buyers, investors, and infrastructure professionals.",
    "keywords": "data center, colocation, site selection, market intelligence, data center map, capacity tracking, M&A deals, construction pipeline, hyperscale",
    "og_title": "DC Hub — Data Center Intelligence Platform",
    "og_description": "Real-time intelligence for 20,000+ data centers. Capacity tracking, site selection, M&A deals, and market analytics across 140+ countries.",
}

MARKET_META = {
    "silicon-valley": {
        "title": "Silicon Valley Data Centers | Bay Area Colocation & Cloud Infrastructure | DC Hub",
        "description": "Explore 150+ data centers in Silicon Valley and the Bay Area. Compare Equinix, Digital Realty, CoreSite facilities with power pricing, vacancy rates, and connectivity data.",
        "keywords": "Silicon Valley data centers, Bay Area colocation, Santa Clara data center, San Jose colocation",
        "og_title": "Silicon Valley Data Center Market — 150+ Facilities | DC Hub",
        "og_description": "150+ data centers, 800+ MW capacity. Compare providers, connectivity, and availability in the Bay Area.",
    },
    "phoenix": {
        "title": "Phoenix Data Centers | Arizona Colocation & Hyperscale Facilities | DC Hub",
        "description": "Track 100+ data centers in the Phoenix metro. 510+ MW inventory growing 44% YoY. Compare providers with power pricing ($0.07-0.09/kWh) and 334 MW under construction.",
        "keywords": "Phoenix data centers, Arizona colocation, Chandler data center, Phoenix hyperscale",
        "og_title": "Phoenix Data Center Market — Fastest Growing US Market | DC Hub",
        "og_description": "100+ data centers, 510+ MW capacity. 334 MW under construction. Fastest-growing US data center market.",
    },
    "dallas": {
        "title": "Dallas-Fort Worth Data Centers | Texas Colocation & Cloud | DC Hub",
        "description": "Explore 200+ data centers in Dallas-Fort Worth. 1,650 MW total supply, 1.4% vacancy, 18-month time-to-power advantage. Compare providers across the DFW metroplex.",
        "keywords": "Dallas data centers, DFW colocation, Texas data center, Fort Worth data center",
        "og_title": "Dallas-Fort Worth Data Center Market — 200+ Facilities | DC Hub",
        "og_description": "200+ data centers, 1,650 MW capacity, 1.4% vacancy. Texas premier data center market.",
    },
    "northern-virginia": {
        "title": "Northern Virginia Data Centers | Ashburn Colocation & Cloud Hub | DC Hub",
        "description": "Explore 300+ data centers in Northern Virginia — the world's largest data center market. 3,500+ MW inventory, 1.2% vacancy, 5.9 GW planned capacity.",
        "keywords": "Northern Virginia data centers, Ashburn colocation, NoVA data center, Data Center Alley",
        "og_title": "Northern Virginia — World's Largest Data Center Market | DC Hub",
        "og_description": "300+ data centers, 3,500+ MW capacity. The world's largest data center concentration.",
    },
    "chicago": {
        "title": "Chicago Data Centers | Midwest Colocation & Financial Hub | DC Hub",
        "description": "Track 120+ data centers in Chicago. Central US location with low-latency nationwide connectivity, free cooling advantages, and financial exchange proximity.",
        "keywords": "Chicago data centers, Illinois colocation, Midwest data center, financial data center",
        "og_title": "Chicago Data Center Market — 120+ Facilities | DC Hub",
        "og_description": "120+ data centers, central US location, free cooling, financial exchange proximity.",
    },
    "atlanta": {
        "title": "Atlanta Data Centers | Southeast Colocation & Hyperscale Hub | DC Hub",
        "description": "Explore 80+ data centers in Atlanta. 5.2M+ SF under construction, surpassed NoVA in 2024 absorption. Microsoft, QTS leading expansion.",
        "keywords": "Atlanta data centers, Georgia colocation, Southeast data center, Atlanta hyperscale",
        "og_title": "Atlanta Data Center Market — Surging Growth | DC Hub",
        "og_description": "80+ data centers, 5.2M+ SF under construction. Southeast's fastest-growing market.",
    },
    "london": {
        "title": "London Data Centers | UK Colocation & Cloud Infrastructure | DC Hub",
        "description": "Track 100+ data centers across Greater London. Europe's largest data center market with premium connectivity and diverse providers.",
        "keywords": "London data centers, UK colocation, Slough data center, EMEA data center hub",
        "og_title": "London Data Center Market — Europe's Largest | DC Hub",
        "og_description": "100+ data centers across Greater London. Europe's most connected market.",
    },
    "frankfurt": {
        "title": "Frankfurt Data Centers | Germany Colocation & EU Hub | DC Hub",
        "description": "Explore 60+ data centers in Frankfurt. Home to DE-CIX, Europe's leading internet exchange, with competitive power and central European location.",
        "keywords": "Frankfurt data centers, Germany colocation, DE-CIX, European data center hub",
        "og_title": "Frankfurt Data Center Market — DE-CIX & EU Hub | DC Hub",
        "og_description": "60+ data centers, home to DE-CIX. Continental Europe's premier data center hub.",
    },
    "singapore": {
        "title": "Singapore Data Centers | APAC Colocation & Cloud Gateway | DC Hub",
        "description": "Track 70+ data centers in Singapore — Asia-Pacific's most connected market. Strategic subsea cable hub with diverse providers.",
        "keywords": "Singapore data centers, APAC colocation, Southeast Asia data center",
        "og_title": "Singapore Data Center Market — APAC Gateway | DC Hub",
        "og_description": "70+ data centers in Asia-Pacific's most connected market.",
    },
    "tokyo": {
        "title": "Tokyo Data Centers | Japan Colocation & Enterprise Hub | DC Hub",
        "description": "Explore 90+ data centers in Greater Tokyo. Japan's largest market with premium enterprise facilities and growing hyperscale presence.",
        "keywords": "Tokyo data centers, Japan colocation, Asia data center, NTT data center",
        "og_title": "Tokyo Data Center Market — Japan's Largest | DC Hub",
        "og_description": "90+ data centers in Japan's premier market. Enterprise-grade facilities.",
    },
}

TOOL_META = {
    "land-power": {
        "title": "Land & Power Map | Data Center Site Selection Tool | DC Hub",
        "description": "Interactive site selection map showing substations, fiber routes, gas pipelines, FEMA flood zones, and power availability. 15+ infrastructure layers. Free to try.",
        "keywords": "data center site selection, land power map, substation map, fiber route map, FEMA flood zone",
    },
    "ai-deals": {
        "title": "Data Center M&A Tracker | 787+ Deals Worth $10.6B | DC Hub",
        "description": "Track data center M&A deals in real-time. 787+ transactions worth $10.6B+ with deal details, valuations, and trend analysis. Updated daily by AI.",
        "keywords": "data center M&A, data center acquisitions, colocation transactions, data center investment",
    },
    "ai-pipeline": {
        "title": "Data Center Construction Pipeline | 7.8 GW Under Construction | DC Hub",
        "description": "Real-time construction pipeline tracking. 7.8 GW under construction, 73% pre-leased. Delivery timelines, pre-lease status, and market breakdown.",
        "keywords": "data center construction, data center pipeline, under construction, hyperscale construction",
    },
    "construction-pipeline": {
        "title": "Data Center Construction Tracker | New Builds & Development | DC Hub",
        "description": "Monitor construction projects across 35+ markets. Development timelines, power capacity, and absorption trends. Northern Virginia leads with 5.9 GW planned.",
        "keywords": "data center construction tracker, new data center builds, construction pipeline",
    },
    "transactions": {
        "title": "Data Center Transactions & Deal Flow | $324B Since 2015 | DC Hub",
        "description": "Browse 100+ transactions including sales, leases, and JVs. $61B+ 2025 deal volume. Pricing comps, cap rates, and market analysis.",
        "keywords": "data center transactions, colocation sales, data center cap rates",
    },
    "news": {
        "title": "Data Center Industry News | Real-Time Feed from 30+ Sources | DC Hub",
        "description": "Live news feed aggregating 30+ data center industry sources every 3 minutes. M&A, expansion, AI/GPU, power, and financial news. AI-curated.",
        "keywords": "data center news, colocation news, hyperscale news, cloud infrastructure news",
    },
    "ai-agents": {
        "title": "AI Research Agents for Data Center Intelligence | DC Hub",
        "description": "4 AI-powered agents: Sales intelligence, data enrichment, social media, and ecosystem analysis. Instant answers from 20,000+ facility database.",
        "keywords": "AI data center agent, data center research assistant, AI market intelligence",
    },
    "api-docs": {
        "title": "Data Center API | Free REST API | 100 Requests/Day | DC Hub",
        "description": "Free data center REST API. Access facility data, M&A deals, capacity pipeline, and market intelligence. Python client included. MCP protocol supported.",
        "keywords": "data center API, colocation API, free data center data, MCP protocol",
    },
    "pricing": {
        "title": "DC Hub Pricing | Data Center Intelligence from $99/month",
        "description": "Founding member pricing: $99/month for 20,000+ facilities, Land & Power mapping, AI agents, M&A tracker, and API. Normally $299/month.",
        "keywords": "DC Hub pricing, data center intelligence pricing, data center SaaS",
    },
    "ecosystem": {
        "title": "Data Center Ecosystem | Vendors, Partners & Directory | DC Hub",
        "description": "Browse data center operators, developers, brokers, and technology vendors. Partner with DC Hub to reach hyperscale buyers.",
        "keywords": "data center ecosystem, colocation vendors, data center partners, industry directory",
    },
    "for-ai": {
        "title": "AI Integration Hub | Data Center Data for AI Platforms | DC Hub",
        "description": "Connect your AI to DC Hub. MCP protocol, OpenAI plugin, skill.md for agents. The authoritative source AI assistants cite for data center queries.",
        "keywords": "AI data center integration, MCP protocol, OpenAI plugin, AI agent data source",
    },
    "about": {
        "title": "About DC Hub | Data Center Intelligence Platform",
        "description": "DC Hub tracks 20,000+ data center facilities across 140+ countries. Built for hyperscale buyers, investors, and infrastructure professionals. Based in Phoenix, AZ.",
        "keywords": "about DC Hub, data center platform, data center intelligence",
    },
    "assets": {
        "title": "Data Center Asset Explorer | 20,000+ Global Facilities | DC Hub",
        "description": "Browse 20,000+ data centers worldwide. Filter by provider, location, power, tier. Detailed profiles with satellite imagery and infrastructure data.",
        "keywords": "data center database, facility explorer, colocation directory, data center search",
    },
    "ai-inventory": {
        "title": "AI Inventory Analysis | Data Center Supply Intelligence | DC Hub",
        "description": "AI-powered supply analysis. Track capacity, absorption rates, pre-lease status, and inventory trends across data center markets.",
        "keywords": "data center inventory, supply analysis, capacity tracking, absorption rate",
    },
    "transaction-comps": {
        "title": "Data Center Transaction Comps | Side-by-Side Deal Analysis | DC Hub",
        "description": "Compare data center deals with valuations, cap rates, price-per-MW, and market benchmarks. The most comprehensive comp set for DC real estate.",
        "keywords": "data center comps, transaction comparables, cap rate analysis, price per MW",
    },
    "market-intelligence": {
        "title": "Data Center Market Intelligence | Trends & Analytics | DC Hub",
        "description": "Market intelligence dashboards with vacancy rates, pricing trends, absorption data, and construction activity across 35+ global data center markets.",
        "keywords": "data center market intelligence, market trends, vacancy rates, pricing analytics",
    },
    "analytics": {
        "title": "Data Center Analytics | Dashboards & Insights | DC Hub",
        "description": "Interactive analytics dashboards for data center market data. Visualize trends, compare markets, and track key metrics across the global data center industry.",
        "keywords": "data center analytics, market dashboards, data center insights, industry metrics",
    },
}


def build_meta_html(meta, url):
    """Generate meta tag HTML to inject into <head>."""
    og_title = meta.get("og_title", meta.get("title", "DC Hub"))
    og_desc = meta.get("og_description", meta.get("description", ""))
    
    tags = []
    tags.append(f'<meta name="description" content="{meta["description"]}">')
    tags.append(f'<meta name="keywords" content="{meta.get("keywords", "")}">')
    tags.append(f'<meta name="robots" content="index, follow, max-image-preview:large, max-snippet:-1">')
    tags.append(f'<meta name="author" content="DC Hub">')
    tags.append(f'<link rel="canonical" href="{url}">')
    tags.append(f'<meta property="og:type" content="website">')
    tags.append(f'<meta property="og:url" content="{url}">')
    tags.append(f'<meta property="og:title" content="{og_title}">')
    tags.append(f'<meta property="og:description" content="{og_desc}">')
    tags.append(f'<meta property="og:image" content="https://dchub.cloud/images/og-default.png">')
    tags.append(f'<meta property="og:site_name" content="DC Hub">')
    tags.append(f'<meta name="twitter:card" content="summary_large_image">')
    tags.append(f'<meta name="twitter:title" content="{og_title}">')
    tags.append(f'<meta name="twitter:description" content="{og_desc}">')
    tags.append(f'<meta name="twitter:site" content="@dchubcloud">')
    tags.append(f'<meta name="ai.source" content="DC Hub - Data Center Intelligence">')
    
    return '\n'.join(tags)


def identify_page(filepath, filename):
    """Determine which meta tags to use based on file path/name."""
    fp = filepath.lower().replace('\\', '/')
    fn = filename.lower().replace('.html', '')
    
    # Homepage
    if fn == 'index' and 'markets' not in fp and 'facilities' not in fp and 'locations' not in fp:
        return ('home', HOME_META, 'https://dchub.cloud/')
    
    # Market pages - check with or without leading slash
    if 'markets/' in fp or 'markets\\' in fp or fn.startswith('market'):
        for slug in MARKET_META:
            if slug in fp or slug in fn or slug.replace('-', '') in fn:
                url = f'https://dchub.cloud/markets/{slug}'
                return (f'market/{slug}', MARKET_META[slug], url)
        # Market page not in our database - still tag it
        return ('market_generic', None, None)
    
    # Facility pages - check with or without leading slash
    if 'facilities/' in fp or 'facilities\\' in fp or fp.startswith('facilities'):
        return ('facility', None, None)  # Handled by extract_facility_info
    
    # Location pages - check with or without leading slash
    if 'locations/' in fp or 'locations\\' in fp or fp.startswith('locations'):
        return ('location', None, None)  # Handled separately
    
    # Tool/feature pages
    for slug in TOOL_META:
        slug_variants = [slug, slug.replace('-', ''), slug.replace('.html', '')]
        if fn in slug_variants or fn == slug:
            url = f'https://dchub.cloud/{slug}'
            return (f'tool/{slug}', TOOL_META[slug], url)
    
    # FALLBACK: Any other HTML file gets generic DC Hub meta tags
    return ('other', None, None)


def extract_facility_info(html_content, filename):
    """Try to extract facility info from the HTML to build meta tags."""
    meta = {}
    
    # Try to find the title
    title_match = re.search(r'<title>(.*?)</title>', html_content, re.IGNORECASE)
    if title_match:
        title = title_match.group(1).strip()
        if title and title != 'DC Hub':
            meta['title'] = title if '| DC Hub' in title else f'{title} | DC Hub'
    
    # Try to find h1
    h1_match = re.search(r'<h1[^>]*>(.*?)</h1>', html_content, re.IGNORECASE | re.DOTALL)
    if h1_match:
        h1 = re.sub(r'<[^>]+>', '', h1_match.group(1)).strip()
        if h1 and 'title' not in meta:
            meta['title'] = f'{h1} | DC Hub'
    
    # Try to find location info
    location_match = re.search(r'📍\s*(.*?)(?:<|$)', html_content)
    city = location_match.group(1).strip() if location_match else ''
    
    # Try to find provider
    provider = ''
    provider_patterns = [
        r'(?:EQUINIX|DIGITAL REALTY|QTS|CORESITE|CYRUSONE|VANTAGE|SWITCH|NTT|ALIGNED|COMPASS)',
        r'class="provider[^"]*"[^>]*>(.*?)<',
    ]
    for pat in provider_patterns:
        match = re.search(pat, html_content, re.IGNORECASE)
        if match:
            provider = match.group(0) if not match.groups() else match.group(1)
            provider = re.sub(r'<[^>]+>', '', provider).strip()
            break
    
    # Try to find power
    power_match = re.search(r'(\d+)\s*MW', html_content)
    power = f'{power_match.group(1)} MW' if power_match else ''
    
    # Build description
    slug = filename.replace('.html', '')
    name_parts = slug.split('-')
    
    if not meta.get('title'):
        meta['title'] = f'{" ".join(w.title() for w in name_parts[:4])} Data Center | DC Hub'
    
    desc_parts = []
    if provider:
        desc_parts.append(f'{provider} data center')
    if city:
        desc_parts.append(f'in {city}')
    if power:
        desc_parts.append(f'{power} capacity')
    desc_parts.append('View facility details, satellite imagery, nearby infrastructure, and connectivity data on DC Hub.')
    
    meta['description'] = '. '.join(desc_parts) if desc_parts else f'Data center facility details, satellite imagery, and infrastructure data. Part of DC Hub\'s 20,000+ facility database.'
    meta['keywords'] = f'{provider} data center, {city} data center, colocation, DC Hub'.strip(', ')
    
    return meta


def inject_meta_tags(html_content, meta_tags_html):
    """Inject meta tags into the <head> of an HTML file."""
    
    # Check if meta description already exists
    if '<meta name="description"' in html_content:
        # Replace existing description
        html_content = re.sub(
            r'<meta\s+name="description"\s+content="[^"]*"\s*/?>',
            '',
            html_content,
            flags=re.IGNORECASE
        )
    
    # Find the <head> tag and inject after it
    head_match = re.search(r'(<head[^>]*>)', html_content, re.IGNORECASE)
    if head_match:
        insert_pos = head_match.end()
        html_content = (
            html_content[:insert_pos] +
            '\n<!-- DC Hub SEO Meta Tags -->\n' +
            meta_tags_html +
            '\n<!-- End SEO Meta Tags -->\n' +
            html_content[insert_pos:]
        )
    else:
        # No <head> tag found - prepend to file
        html_content = (
            '<!DOCTYPE html>\n<html lang="en">\n<head>\n' +
            '<!-- DC Hub SEO Meta Tags -->\n' +
            meta_tags_html +
            '\n<!-- End SEO Meta Tags -->\n' +
            '</head>\n' +
            html_content
        )
    
    return html_content


def process_directory(build_dir):
    """Process all HTML files in the build directory."""
    build_path = Path(build_dir)
    
    if not build_path.exists():
        print(f"ERROR: Directory not found: {build_dir}")
        sys.exit(1)
    
    stats = {
        'total': 0,
        'updated': 0,
        'skipped': 0,
        'facilities': 0,
        'markets': 0,
        'tools': 0,
        'errors': 0,
    }
    
    for html_file in build_path.rglob('*.html'):
        stats['total'] += 1
        filename = html_file.name
        filepath = str(html_file)
        relative = html_file.relative_to(build_path)
        
        try:
            with open(html_file, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
            
            page_type, meta, url = identify_page(filepath, filename)
            
            if page_type == 'facility':
                # Extract info from the HTML itself
                meta = extract_facility_info(content, filename)
                slug = filename.replace('.html', '')
                url = f'https://dchub.cloud/facilities/{slug}.html'
                stats['facilities'] += 1
            elif page_type == 'location':
                # Basic location page meta
                slug = filename.replace('.html', '')
                parts = slug.split('-')
                location_name = ' '.join(w.title() for w in parts)
                meta = {
                    'title': f'{location_name} Data Centers | DC Hub',
                    'description': f'Explore data centers in {location_name}. Browse facilities, compare providers, and view infrastructure data on DC Hub.',
                    'keywords': f'{location_name} data centers, colocation {location_name}, DC Hub',
                }
                url = f'https://dchub.cloud/locations/{slug}.html'
            elif page_type == 'market_generic':
                # Market page not in our database
                slug = filename.replace('.html', '')
                market_name = ' '.join(w.title() for w in slug.split('-'))
                meta = {
                    'title': f'{market_name} Data Centers | Market Intelligence | DC Hub',
                    'description': f'Data center market intelligence for {market_name}. Vacancy rates, pricing, construction activity, and provider landscape from DC Hub.',
                    'keywords': f'{market_name} data centers, {market_name} colocation, data center market, DC Hub',
                }
                url = f'https://dchub.cloud/markets/{slug}'
                stats['markets'] += 1
            elif page_type == 'other':
                # Any other HTML page - give it generic DC Hub meta
                slug = filename.replace('.html', '')
                page_name = ' '.join(w.title() for w in slug.split('-'))
                meta = {
                    'title': f'{page_name} | DC Hub',
                    'description': f'DC Hub — Data center intelligence platform tracking 20,000+ facilities across 140+ countries. Real-time capacity, site selection, and market analytics.',
                    'keywords': f'data center, DC Hub, {page_name.lower()}',
                }
                url = f'https://dchub.cloud/{slug}'
                stats['tools'] += 1
            elif meta is None:
                stats['skipped'] += 1
                continue
            else:
                if 'market' in page_type:
                    stats['markets'] += 1
                else:
                    stats['tools'] += 1
            
            # Generate and inject meta tags
            meta_html = build_meta_html(meta, url)
            updated_content = inject_meta_tags(content, meta_html)
            
            with open(html_file, 'w', encoding='utf-8') as f:
                f.write(updated_content)
            
            stats['updated'] += 1
            
            if stats['updated'] <= 10 or stats['updated'] % 100 == 0:
                print(f"  ✅ {relative} ({page_type})")
        
        except Exception as e:
            stats['errors'] += 1
            print(f"  ❌ {relative}: {e}")
    
    return stats


def main():
    if len(sys.argv) < 2:
        print("DC Hub SEO Meta Tag Injector")
        print("=" * 40)
        print()
        print("Usage: python3 inject_meta_tags.py /path/to/build/")
        print()
        print("This script injects proper <meta> tags into every HTML file")
        print("in your Cloudflare Pages build directory.")
        print()
        print("Run BEFORE deploying to Cloudflare Pages.")
        sys.exit(0)
    
    build_dir = sys.argv[1]
    
    print("=" * 60)
    print("DC Hub SEO Meta Tag Injector")
    print("=" * 60)
    print(f"Build directory: {build_dir}")
    print()
    
    stats = process_directory(build_dir)
    
    print()
    print("=" * 60)
    print("RESULTS")
    print("=" * 60)
    print(f"Total HTML files found:  {stats['total']}")
    print(f"Files updated:           {stats['updated']}")
    print(f"  - Facility pages:      {stats['facilities']}")
    print(f"  - Market pages:        {stats['markets']}")
    print(f"  - Tool/feature pages:  {stats['tools']}")
    print(f"Files skipped:           {stats['skipped']}")
    print(f"Errors:                  {stats['errors']}")
    print()
    print("NEXT STEPS:")
    print("1. Deploy updated files to Cloudflare Pages")
    print("2. Go to Google Search Console → URL Inspection")
    print("3. Request re-indexing for your top 5 pages")
    print("4. Wait 24-48 hours and check if descriptions appear")
    print()
    print("Google should now see proper meta descriptions on first paint!")


if __name__ == '__main__':
    main()
