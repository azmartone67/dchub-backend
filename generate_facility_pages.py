#!/usr/bin/env python3
"""
DC HUB FACILITY PAGE GENERATOR
==============================
Generates static HTML pages for SEO indexing.

Creates:
- Individual facility pages (/facilities/[slug].html)
- Location pages (/locations/[country]/[state]/[city].html)
- Updated sitemap.xml with all new URLs

Usage:
  python generate_facility_pages.py

Output:
  ./output/facilities/     - Individual facility pages
  ./output/locations/      - Location hierarchy pages
  ./output/sitemap.xml     - Complete sitemap
"""

import os
import json
import requests
import re
from datetime import datetime
from collections import defaultdict
from html import escape
import hashlib

# Configuration
# When running in Replit alongside your backend, use localhost:
API_BASE = "http://127.0.0.1:5000"
# When running externally, use your public URL:
# API_BASE = "https://dchub.cloud"
OUTPUT_DIR = "./output"
SITE_URL = "https://dchub.cloud"
MAX_FACILITIES = 2000  # Limit for initial generation (can increase later)

# Create output directories
os.makedirs(f"{OUTPUT_DIR}/facilities", exist_ok=True)
os.makedirs(f"{OUTPUT_DIR}/locations", exist_ok=True)

def slugify(text):
    """Convert text to URL-friendly slug"""
    if not text:
        return "unknown"
    text = text.lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[\s_]+', '-', text)
    text = re.sub(r'-+', '-', text)
    return text[:80] or "unknown"

def fetch_facilities():
    """Fetch all facilities from API"""
    print("📡 Fetching facilities from API...")
    all_facilities = []
    page = 1
    
    while len(all_facilities) < MAX_FACILITIES:
        try:
            url = f"{API_BASE}/api/v1/facilities?page={page}&limit=100"
            response = requests.get(url, timeout=30)
            data = response.json()
            
            if not data.get('success') or not data.get('data'):
                break
                
            facilities = data['data']
            if not facilities:
                break
                
            all_facilities.extend(facilities)
            print(f"   Page {page}: {len(facilities)} facilities (total: {len(all_facilities)})")
            
            # Check if we've got all pages
            pagination = data.get('pagination', {})
            if page >= pagination.get('pages', 1):
                break
                
            page += 1
            
        except Exception as e:
            print(f"   ⚠️ Error fetching page {page}: {e}")
            break
    
    print(f"✅ Fetched {len(all_facilities)} facilities total")
    return all_facilities

def generate_facility_id(facility):
    """Generate unique ID for facility"""
    key = f"{facility.get('name', '')}-{facility.get('city', '')}-{facility.get('provider', '')}"
    return hashlib.md5(key.encode()).hexdigest()[:8]

def get_facility_slug(facility):
    """Generate URL slug for facility"""
    provider = slugify(facility.get('provider', 'datacenter'))
    name = slugify(facility.get('name', ''))
    city = slugify(facility.get('city', ''))
    
    if name and name != 'unknown':
        slug = f"{provider}-{name}"
    else:
        slug = f"{provider}-{city}"
    
    # Add unique suffix to prevent collisions
    uid = generate_facility_id(facility)
    return f"{slug}-{uid}"

def format_power(power_mw):
    """Format power capacity"""
    if not power_mw:
        return "N/A"
    try:
        power = float(power_mw)
        if power >= 1:
            return f"{power:.1f} MW"
        else:
            return f"{power * 1000:.0f} kW"
    except:
        return "N/A"

def get_status_badge(status):
    """Get status badge HTML"""
    status = (status or 'unknown').lower()
    colors = {
        'operational': ('#10b981', '✅'),
        'active': ('#10b981', '✅'),
        'under construction': ('#f59e0b', '🏗️'),
        'planned': ('#6366f1', '📋'),
        'unknown': ('#6b7280', '❓')
    }
    color, icon = colors.get(status, colors['unknown'])
    return f'<span style="background:{color};color:white;padding:4px 12px;border-radius:12px;font-size:13px">{icon} {status.title()}</span>'

def generate_facility_html(facility):
    """Generate HTML page for a single facility"""
    name = escape(facility.get('name') or f"{facility.get('provider', 'Data Center')} - {facility.get('city', 'Unknown')}")
    provider = escape(facility.get('provider') or 'Unknown Provider')
    city = escape(facility.get('city') or 'Unknown')
    state = escape(facility.get('state') or '')
    country = escape(facility.get('country') or 'Unknown')
    power_mw = facility.get('power_mw')
    status = facility.get('status') or 'Unknown'
    lat = facility.get('lat') or facility.get('latitude')
    lng = facility.get('lng') or facility.get('longitude')
    region = escape(facility.get('region') or '')
    source = escape(facility.get('source') or 'DC Hub')
    
    location = ', '.join(filter(None, [city, state, country]))
    slug = get_facility_slug(facility)
    canonical_url = f"{SITE_URL}/facilities/{slug}.html"
    
    # Meta description
    meta_desc = f"{name} data center by {provider} in {location}. "
    if power_mw:
        meta_desc += f"Power capacity: {format_power(power_mw)}. "
    meta_desc += "View specs, location, and contact info on DC Hub."
    
    # Schema.org structured data
    schema = {
        "@context": "https://schema.org",
        "@type": "Place",
        "name": name,
        "description": f"Data center facility operated by {provider} in {location}",
        "address": {
            "@type": "PostalAddress",
            "addressLocality": city,
            "addressRegion": state,
            "addressCountry": country
        }
    }
    if lat and lng:
        schema["geo"] = {
            "@type": "GeoCoordinates",
            "latitude": lat,
            "longitude": lng
        }
    
    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{name} - Data Center | DC Hub</title>
    <meta name="description" content="{escape(meta_desc[:160])}">
    <meta name="keywords" content="{provider}, data center, {city}, {country}, colocation, {region}">
    <meta name="robots" content="index, follow">
    <link rel="canonical" href="{canonical_url}">
    
    <!-- Open Graph -->
    <meta property="og:title" content="{name} - Data Center">
    <meta property="og:description" content="{escape(meta_desc[:160])}">
    <meta property="og:type" content="place">
    <meta property="og:url" content="{canonical_url}">
    <meta property="og:site_name" content="DC Hub">
    
    <!-- Twitter -->
    <meta name="twitter:card" content="summary">
    <meta name="twitter:title" content="{name}">
    <meta name="twitter:description" content="{escape(meta_desc[:100])}">
    
    <!-- Schema.org -->
    <script type="application/ld+json">
    {json.dumps(schema, indent=2)}
    </script>
    
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0a0a0f; color: #e0e0e0; line-height: 1.6; }}
        .header {{ background: linear-gradient(135deg, #1a1a2e 0%, #0a0a0f 100%); padding: 20px; border-bottom: 1px solid #333; }}
        .header-inner {{ max-width: 1200px; margin: 0 auto; display: flex; justify-content: space-between; align-items: center; }}
        .logo {{ font-size: 24px; font-weight: 700; color: #fbbf24; text-decoration: none; }}
        .nav a {{ color: #888; text-decoration: none; margin-left: 24px; }}
        .nav a:hover {{ color: #fff; }}
        .breadcrumb {{ max-width: 1200px; margin: 20px auto; padding: 0 20px; font-size: 14px; color: #666; }}
        .breadcrumb a {{ color: #6366f1; text-decoration: none; }}
        .container {{ max-width: 1200px; margin: 0 auto; padding: 20px; }}
        .facility-header {{ background: linear-gradient(135deg, #1e1e2e 0%, #151520 100%); border-radius: 16px; padding: 32px; margin-bottom: 24px; border: 1px solid #333; }}
        .facility-title {{ font-size: 32px; font-weight: 700; margin-bottom: 8px; }}
        .facility-provider {{ color: #fbbf24; font-size: 18px; margin-bottom: 16px; }}
        .facility-location {{ color: #888; font-size: 16px; display: flex; align-items: center; gap: 8px; }}
        .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 24px; }}
        .stat-card {{ background: #1a1a2e; border-radius: 12px; padding: 20px; border: 1px solid #333; }}
        .stat-label {{ color: #888; font-size: 13px; margin-bottom: 4px; }}
        .stat-value {{ font-size: 24px; font-weight: 600; color: #fff; }}
        .map-container {{ background: #1a1a2e; border-radius: 12px; height: 400px; margin-bottom: 24px; border: 1px solid #333; overflow: hidden; }}
        .map-placeholder {{ height: 100%; display: flex; align-items: center; justify-content: center; color: #666; }}
        .cta-section {{ background: linear-gradient(135deg, #6366f1 0%, #4f46e5 100%); border-radius: 12px; padding: 32px; text-align: center; margin-bottom: 24px; }}
        .cta-section h3 {{ font-size: 24px; margin-bottom: 12px; }}
        .cta-section p {{ opacity: 0.9; margin-bottom: 20px; }}
        .cta-btn {{ display: inline-block; background: #fff; color: #4f46e5; padding: 12px 32px; border-radius: 8px; text-decoration: none; font-weight: 600; }}
        .cta-btn:hover {{ background: #f0f0f0; }}
        .footer {{ background: #0a0a0f; border-top: 1px solid #222; padding: 40px 20px; text-align: center; color: #666; margin-top: 40px; }}
        .footer a {{ color: #6366f1; text-decoration: none; }}
        .related {{ margin-top: 32px; }}
        .related h3 {{ margin-bottom: 16px; }}
        .related-links {{ display: flex; flex-wrap: wrap; gap: 12px; }}
        .related-links a {{ background: #1a1a2e; padding: 8px 16px; border-radius: 8px; color: #6366f1; text-decoration: none; border: 1px solid #333; }}
        .related-links a:hover {{ background: #252540; }}
    </style>
</head>
<body>
    <header class="header">
        <div class="header-inner">
            <a href="{SITE_URL}/" class="logo">⚡ DC Hub</a>
            <nav class="nav">
                <a href="{SITE_URL}/land-power.html">Land & Power</a>
                <a href="{SITE_URL}/market-intelligence.html">Markets</a>
                <a href="{SITE_URL}/transactions.html">Transactions</a>
                <a href="{SITE_URL}/news.html">News</a>
            </nav>
        </div>
    </header>
    
    <div class="breadcrumb">
        <a href="{SITE_URL}/">Home</a> &rsaquo; 
        <a href="{SITE_URL}/locations/{slugify(country)}.html">{country}</a> &rsaquo;
        {f'<a href="{SITE_URL}/locations/{slugify(country)}/{slugify(state)}.html">{state}</a> &rsaquo;' if state else ''}
        <span>{name}</span>
    </div>
    
    <div class="container">
        <div class="facility-header">
            <h1 class="facility-title">{name}</h1>
            <div class="facility-provider">🏢 {provider}</div>
            <div class="facility-location">
                📍 {location}
                <span style="margin-left: 16px">{get_status_badge(status)}</span>
            </div>
        </div>
        
        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-label">Power Capacity</div>
                <div class="stat-value">⚡ {format_power(power_mw)}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Status</div>
                <div class="stat-value">{status.title() if status else 'Unknown'}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Region</div>
                <div class="stat-value">🌍 {region or country}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Data Source</div>
                <div class="stat-value">📊 {source}</div>
            </div>
        </div>
        
        {'<div class="map-container"><iframe src="https://maps.google.com/maps?q=' + str(lat) + ',' + str(lng) + '&z=14&output=embed" width="100%" height="100%" style="border:0" loading="lazy"></iframe></div>' if lat and lng else '<div class="map-container"><div class="map-placeholder">📍 Location map not available</div></div>'}
        
        <div class="cta-section">
            <h3>Need More Details?</h3>
            <p>Access comprehensive facility data, power availability, and market intelligence.</p>
            <a href="{SITE_URL}/land-power.html" class="cta-btn">Explore Land & Power Map</a>
        </div>
        
        <div class="related">
            <h3>Explore More</h3>
            <div class="related-links">
                <a href="{SITE_URL}/locations/{slugify(country)}.html">More in {country}</a>
                {f'<a href="{SITE_URL}/locations/{slugify(country)}/{slugify(state)}.html">More in {state}</a>' if state else ''}
                <a href="{SITE_URL}/market-intelligence.html">Market Intelligence</a>
                <a href="{SITE_URL}/transactions.html">Recent Transactions</a>
            </div>
        </div>
    </div>
    
    <footer class="footer">
        <p>© 2026 <a href="{SITE_URL}/">DC Hub</a> - Data Center Intelligence Platform</p>
        <p style="margin-top: 8px; font-size: 13px">Tracking 20,000+ facilities across 140+ countries</p>
    </footer>
</body>
</html>'''
    
    return html, slug

def generate_location_html(location_type, name, facilities, parent_path=""):
    """Generate HTML page for a location (country/state/city)"""
    count = len(facilities)
    
    if location_type == "country":
        title = f"Data Centers in {name}"
        breadcrumb = f'<a href="{SITE_URL}/">Home</a> &rsaquo; <span>{name}</span>'
    elif location_type == "state":
        country = parent_path.split('/')[0] if '/' in parent_path else parent_path
        title = f"Data Centers in {name}"
        breadcrumb = f'<a href="{SITE_URL}/">Home</a> &rsaquo; <a href="{SITE_URL}/locations/{slugify(country)}.html">{country}</a> &rsaquo; <span>{name}</span>'
    else:
        title = f"Data Centers in {name}"
        breadcrumb = f'<a href="{SITE_URL}/">Home</a> &rsaquo; <span>{name}</span>'
    
    # Calculate total power
    total_power = sum(f.get('power_mw') or 0 for f in facilities)
    
    # Get top providers
    providers = defaultdict(int)
    for f in facilities:
        if f.get('provider'):
            providers[f['provider']] += 1
    top_providers = sorted(providers.items(), key=lambda x: -x[1])[:10]
    
    # Generate facility list HTML
    facility_rows = ""
    for f in facilities[:100]:  # Limit to 100 per page
        slug = get_facility_slug(f)
        fname = escape(f.get('name') or f.get('provider', 'Unknown'))
        fprovider = escape(f.get('provider') or 'Unknown')
        fcity = escape(f.get('city') or '')
        fpower = format_power(f.get('power_mw'))
        fstatus = (f.get('status') or 'unknown').title()
        
        facility_rows += f'''
        <tr>
            <td><a href="{SITE_URL}/facilities/{slug}.html">{fname}</a></td>
            <td>{fprovider}</td>
            <td>{fcity}</td>
            <td>{fpower}</td>
            <td>{fstatus}</td>
        </tr>'''
    
    # Generate top providers HTML
    providers_html = ""
    for provider, pcount in top_providers:
        providers_html += f'<span style="background:#1a1a2e;padding:6px 12px;border-radius:6px;margin:4px;display:inline-block">{escape(provider)} ({pcount})</span>'
    
    meta_desc = f"Browse {count} data center facilities in {name}. Total power capacity: {format_power(total_power)}. Find colocation, cloud, and hyperscale facilities."
    
    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title} - {count} Facilities | DC Hub</title>
    <meta name="description" content="{escape(meta_desc[:160])}">
    <meta name="robots" content="index, follow">
    <link rel="canonical" href="{SITE_URL}/locations/{slugify(name)}.html">
    
    <meta property="og:title" content="{title}">
    <meta property="og:description" content="{escape(meta_desc[:160])}">
    <meta property="og:type" content="website">
    <meta property="og:site_name" content="DC Hub">
    
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0a0a0f; color: #e0e0e0; line-height: 1.6; }}
        .header {{ background: linear-gradient(135deg, #1a1a2e 0%, #0a0a0f 100%); padding: 20px; border-bottom: 1px solid #333; }}
        .header-inner {{ max-width: 1200px; margin: 0 auto; display: flex; justify-content: space-between; align-items: center; }}
        .logo {{ font-size: 24px; font-weight: 700; color: #fbbf24; text-decoration: none; }}
        .nav a {{ color: #888; text-decoration: none; margin-left: 24px; }}
        .breadcrumb {{ max-width: 1200px; margin: 20px auto; padding: 0 20px; font-size: 14px; color: #666; }}
        .breadcrumb a {{ color: #6366f1; text-decoration: none; }}
        .container {{ max-width: 1200px; margin: 0 auto; padding: 20px; }}
        .page-header {{ background: linear-gradient(135deg, #1e1e2e 0%, #151520 100%); border-radius: 16px; padding: 32px; margin-bottom: 24px; border: 1px solid #333; }}
        .page-title {{ font-size: 32px; font-weight: 700; margin-bottom: 16px; }}
        .stats {{ display: flex; gap: 32px; flex-wrap: wrap; }}
        .stat {{ text-align: center; }}
        .stat-num {{ font-size: 28px; font-weight: 700; color: #fbbf24; }}
        .stat-label {{ font-size: 13px; color: #888; }}
        .providers {{ margin: 24px 0; }}
        .providers h3 {{ margin-bottom: 12px; font-size: 16px; color: #888; }}
        table {{ width: 100%; border-collapse: collapse; background: #1a1a2e; border-radius: 12px; overflow: hidden; }}
        th, td {{ padding: 12px 16px; text-align: left; border-bottom: 1px solid #333; }}
        th {{ background: #252540; color: #888; font-weight: 600; font-size: 13px; text-transform: uppercase; }}
        td a {{ color: #6366f1; text-decoration: none; }}
        td a:hover {{ text-decoration: underline; }}
        .footer {{ background: #0a0a0f; border-top: 1px solid #222; padding: 40px 20px; text-align: center; color: #666; margin-top: 40px; }}
        .footer a {{ color: #6366f1; text-decoration: none; }}
    </style>
</head>
<body>
    <header class="header">
        <div class="header-inner">
            <a href="{SITE_URL}/" class="logo">⚡ DC Hub</a>
            <nav class="nav">
                <a href="{SITE_URL}/land-power.html">Land & Power</a>
                <a href="{SITE_URL}/market-intelligence.html">Markets</a>
                <a href="{SITE_URL}/transactions.html">Transactions</a>
            </nav>
        </div>
    </header>
    
    <div class="breadcrumb">{breadcrumb}</div>
    
    <div class="container">
        <div class="page-header">
            <h1 class="page-title">📍 {title}</h1>
            <div class="stats">
                <div class="stat">
                    <div class="stat-num">{count}</div>
                    <div class="stat-label">Facilities</div>
                </div>
                <div class="stat">
                    <div class="stat-num">{format_power(total_power)}</div>
                    <div class="stat-label">Total Power</div>
                </div>
                <div class="stat">
                    <div class="stat-num">{len(providers)}</div>
                    <div class="stat-label">Providers</div>
                </div>
            </div>
        </div>
        
        <div class="providers">
            <h3>Top Providers</h3>
            {providers_html}
        </div>
        
        <table>
            <thead>
                <tr>
                    <th>Facility</th>
                    <th>Provider</th>
                    <th>City</th>
                    <th>Power</th>
                    <th>Status</th>
                </tr>
            </thead>
            <tbody>
                {facility_rows}
            </tbody>
        </table>
        
        {f'<p style="margin-top:16px;color:#888">Showing 100 of {count} facilities</p>' if count > 100 else ''}
    </div>
    
    <footer class="footer">
        <p>© 2026 <a href="{SITE_URL}/">DC Hub</a> - Data Center Intelligence Platform</p>
    </footer>
</body>
</html>'''
    
    return html

def generate_sitemap(facility_slugs, location_slugs):
    """Generate sitemap.xml with all URLs"""
    urls = []
    today = datetime.now().strftime('%Y-%m-%d')
    
    # Static pages
    static_pages = [
        ('/', '1.0', 'daily'),
        ('/land-power.html', '0.9', 'daily'),
        ('/market-intelligence.html', '0.9', 'daily'),
        ('/transactions.html', '0.9', 'daily'),
        ('/construction-pipeline.html', '0.9', 'daily'),
        ('/news.html', '0.8', 'hourly'),
        ('/compare.html', '0.8', 'weekly'),
        ('/analytics.html', '0.8', 'daily'),
        ('/ai-agents.html', '0.7', 'weekly'),
        ('/pricing.html', '0.7', 'monthly'),
        ('/about.html', '0.6', 'monthly'),
        ('/api-docs.html', '0.6', 'monthly'),
        ('/privacy.html', '0.3', 'yearly'),
        ('/terms.html', '0.3', 'yearly'),
    ]
    
    for path, priority, freq in static_pages:
        urls.append(f'''  <url>
    <loc>{SITE_URL}{path}</loc>
    <lastmod>{today}</lastmod>
    <changefreq>{freq}</changefreq>
    <priority>{priority}</priority>
  </url>''')
    
    # Facility pages
    for slug in facility_slugs:
        urls.append(f'''  <url>
    <loc>{SITE_URL}/facilities/{slug}.html</loc>
    <lastmod>{today}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>0.6</priority>
  </url>''')
    
    # Location pages
    for slug in location_slugs:
        urls.append(f'''  <url>
    <loc>{SITE_URL}/locations/{slug}.html</loc>
    <lastmod>{today}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>0.7</priority>
  </url>''')
    
    sitemap = f'''<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{chr(10).join(urls)}
</urlset>'''
    
    return sitemap

def main():
    print("=" * 60)
    print("DC HUB FACILITY PAGE GENERATOR")
    print("=" * 60)
    print()
    
    # Fetch facilities
    facilities = fetch_facilities()
    
    if not facilities:
        print("❌ No facilities found. Check API connection.")
        return
    
    # Generate facility pages
    print()
    print("📝 Generating facility pages...")
    facility_slugs = []
    
    for i, facility in enumerate(facilities):
        try:
            html, slug = generate_facility_html(facility)
            filepath = f"{OUTPUT_DIR}/facilities/{slug}.html"
            
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(html)
            
            facility_slugs.append(slug)
            
            if (i + 1) % 100 == 0:
                print(f"   Generated {i + 1}/{len(facilities)} facility pages...")
                
        except Exception as e:
            print(f"   ⚠️ Error generating page for facility: {e}")
    
    print(f"✅ Generated {len(facility_slugs)} facility pages")
    
    # Group facilities by location
    print()
    print("📍 Generating location pages...")
    
    by_country = defaultdict(list)
    by_state = defaultdict(list)
    
    for f in facilities:
        country = f.get('country')
        state = f.get('state')
        
        if country:
            by_country[country].append(f)
            if state:
                by_state[f"{country}/{state}"].append(f)
    
    # Generate country pages
    location_slugs = []
    
    for country, country_facilities in by_country.items():
        if len(country_facilities) >= 3:  # Only generate if 3+ facilities
            try:
                html = generate_location_html("country", country, country_facilities)
                slug = slugify(country)
                filepath = f"{OUTPUT_DIR}/locations/{slug}.html"
                
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(html)
                
                location_slugs.append(slug)
            except Exception as e:
                print(f"   ⚠️ Error generating country page for {country}: {e}")
    
    # Generate state pages
    for state_key, state_facilities in by_state.items():
        if len(state_facilities) >= 3:  # Only generate if 3+ facilities
            try:
                country, state = state_key.split('/')
                html = generate_location_html("state", state, state_facilities, country)
                slug = f"{slugify(country)}-{slugify(state)}"
                filepath = f"{OUTPUT_DIR}/locations/{slug}.html"
                
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(html)
                
                location_slugs.append(slug)
            except Exception as e:
                print(f"   ⚠️ Error generating state page: {e}")
    
    print(f"✅ Generated {len(location_slugs)} location pages")
    
    # Generate sitemap
    print()
    print("🗺️ Generating sitemap...")
    
    sitemap = generate_sitemap(facility_slugs, location_slugs)
    
    with open(f"{OUTPUT_DIR}/sitemap.xml", 'w', encoding='utf-8') as f:
        f.write(sitemap)
    
    print(f"✅ Generated sitemap with {len(facility_slugs) + len(location_slugs) + 14} URLs")
    
    # Summary
    print()
    print("=" * 60)
    print("✅ GENERATION COMPLETE!")
    print("=" * 60)
    print()
    print(f"📁 Output directory: {OUTPUT_DIR}/")
    print(f"   📄 Facility pages: {len(facility_slugs)}")
    print(f"   📍 Location pages: {len(location_slugs)}")
    print(f"   🗺️ Sitemap URLs: {len(facility_slugs) + len(location_slugs) + 14}")
    print()
    print("📋 Next steps:")
    print("   1. Upload entire 'output' folder to Cloudflare Pages")
    print("   2. Submit new sitemap.xml to Google Search Console")
    print("   3. Request indexing for key pages")
    print()

if __name__ == "__main__":
    main()
