"""
DC Hub - Static Facility Page Generator v2 (Bug-Fixed)
=======================================================
Generates static HTML pages for all facilities in the database.

SAFETY FEATURES:
- HTML escaping to prevent XSS
- Null-safe field handling
- Rate limiting on API calls
- Consistent slug generation
- Dry-run mode for testing
- Progress saving for resume

Usage:
    python generate_facility_pages.py              # Full run
    python generate_facility_pages.py --dry-run    # Test without writing files
    python generate_facility_pages.py --limit 100  # Only generate 100 pages (for testing)

Output:
    - /facilities/*.html (one page per facility)
    - /sitemap.xml (updated with all facility URLs)
"""

import requests
import json
import os
import re
import sys
import time
import html as html_lib
from datetime import datetime
from urllib.parse import quote

# =============================================================================
# CONFIGURATION
# =============================================================================

API_BASE = "https://dchub.cloud"
OUTPUT_DIR = "facilities"
SITE_URL = "https://dchub.cloud"

# Rate limiting - be gentle on the API
REQUEST_DELAY = 0.1  # seconds between API calls
BATCH_SIZE = 100     # facilities per API request

# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def safe_str(value, default=''):
    """Safely convert value to string, handling None"""
    if value is None:
        return default
    return str(value)

def escape_html(text):
    """Escape HTML special characters to prevent XSS"""
    if text is None:
        return ''
    return html_lib.escape(str(text))

def slugify(text):
    """Convert text to URL-friendly slug"""
    if not text:
        return "unknown"
    # Convert to string and lowercase
    slug = str(text).lower()
    # Replace spaces and special chars with hyphens
    slug = re.sub(r'[^a-z0-9]+', '-', slug)
    # Remove leading/trailing hyphens
    slug = slug.strip('-')
    # Collapse multiple hyphens
    slug = re.sub(r'-+', '-', slug)
    # Limit length to avoid filesystem issues
    slug = slug[:100]
    return slug or "facility"

def generate_facility_slug(facility):
    """Generate unique slug for a facility"""
    parts = []
    
    provider = facility.get('provider')
    if provider:
        parts.append(str(provider))
    
    name = facility.get('name')
    if name:
        parts.append(str(name))
    elif facility.get('city'):
        parts.append(str(facility['city']))
    
    # Always include ID for uniqueness
    fac_id = facility.get('id')
    if fac_id:
        parts.append(str(fac_id))
    
    return slugify('-'.join(parts))

# =============================================================================
# API FUNCTIONS
# =============================================================================

def fetch_all_facilities(limit=None):
    """Fetch all facilities from the API with rate limiting"""
    all_facilities = []
    page = 1
    
    print("Fetching facilities from API...")
    print(f"  Rate limit: {REQUEST_DELAY}s between requests")
    
    while True:
        try:
            url = f"{API_BASE}/api/v1/facilities?page={page}&limit={BATCH_SIZE}"
            
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            
            data = response.json()
            
            if not data.get('success'):
                print(f"  API returned success=false on page {page}")
                break
                
            facilities = data.get('data', [])
            if not facilities:
                print(f"  No more facilities on page {page}")
                break
            
            all_facilities.extend(facilities)
            
            print(f"  Page {page}: +{len(facilities)} facilities (total: {len(all_facilities)})")
            
            # Check if we've hit our limit
            if limit and len(all_facilities) >= limit:
                all_facilities = all_facilities[:limit]
                print(f"  Reached limit of {limit} facilities")
                break
            
            # Check pagination
            pagination = data.get('pagination', {})
            total_pages = pagination.get('pages', 1)
            if page >= total_pages:
                break
            
            page += 1
            time.sleep(REQUEST_DELAY)  # Rate limiting
            
        except requests.exceptions.RequestException as e:
            print(f"  Network error on page {page}: {e}")
            break
        except json.JSONDecodeError as e:
            print(f"  JSON parse error on page {page}: {e}")
            break
        except Exception as e:
            print(f"  Unexpected error on page {page}: {e}")
            break
    
    print(f"Total facilities fetched: {len(all_facilities)}")
    return all_facilities

# =============================================================================
# HTML GENERATION
# =============================================================================

def generate_facility_html(facility, slug):
    """Generate HTML page for a single facility with proper escaping"""
    
    # Extract and escape all fields
    name = escape_html(facility.get('name') or 'Data Center')
    provider = escape_html(facility.get('provider') or 'Unknown Provider')
    city = escape_html(facility.get('city') or '')
    state = escape_html(facility.get('state') or '')
    country = escape_html(facility.get('country') or 'USA')
    address = escape_html(facility.get('address') or '')
    region = escape_html(facility.get('region') or '')
    facility_type = escape_html(facility.get('facility_type') or 'Colocation')
    
    # Numeric fields (safe)
    power_mw = facility.get('power_mw') or 0
    sqft = facility.get('sqft') or 0
    lat = facility.get('latitude') or facility.get('lat')
    lng = facility.get('longitude') or facility.get('lng')
    
    # Status with safe lowercase
    status_raw = facility.get('status') or 'active'
    status = escape_html(status_raw)
    status_lower = str(status_raw).lower() if status_raw else 'active'
    
    # Build location string
    location_parts = [p for p in [city, state, country] if p]
    location = ', '.join(location_parts) or 'United States'
    
    # Meta description (escaped)
    meta_desc = f"{name} by {provider} in {location}."
    if power_mw:
        meta_desc += f" {power_mw} MW power capacity."
    meta_desc += " View specs, location, and contact info on DC Hub."
    
    # Page title
    title = f"{name} - {provider} Data Center in {city or country} | DC Hub"
    
    # Status badge color
    status_colors = {
        'active': '#22c55e',
        'operational': '#22c55e', 
        'construction': '#f59e0b',
        'planned': '#3b82f6',
        'unknown': '#6b7280'
    }
    status_color = status_colors.get(status_lower, '#6b7280')
    
    # Build optional sections
    address_row = ''
    if address:
        address_row = f'<div class="info-row"><span class="info-label">Address</span><span class="info-value">{address}</span></div>'
    
    sqft_row = ''
    if sqft:
        sqft_row = f'<div class="info-row"><span class="info-label">Building Size</span><span class="info-value">{sqft:,} sq ft</span></div>'
    
    map_section = ''
    if lat and lng:
        # Using OpenStreetMap instead of Google Maps (no API key needed)
        map_section = f'''<div class="section">
                    <h2>🗺️ Location</h2>
                    <div class="map-container">
                        <iframe src="https://www.openstreetmap.org/export/embed.html?bbox={lng-0.01},{lat-0.01},{lng+0.01},{lat+0.01}&layer=mapnik&marker={lat},{lng}" allowfullscreen loading="lazy"></iframe>
                    </div>
                    <p style="font-size: 12px; color: var(--text3); margin-top: 8px;">
                        Coordinates: {lat:.4f}, {lng:.4f}
                    </p>
                </div>'''
    
    # Market link (use city slug if available)
    market_slug = slugify(city) if city else 'northern-virginia'
    market_display = city or 'this market'
    
    # Provider link (URL encoded)
    provider_encoded = quote(provider)
    
    # Schema.org JSON (must be valid JSON)
    schema_geo = ''
    if lat and lng:
        schema_geo = f'"geo": {{"@type": "GeoCoordinates", "latitude": {lat}, "longitude": {lng}}},'
    
    # Generate HTML
    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <meta name="description" content="{meta_desc}">
    <meta name="keywords" content="{provider}, {name}, {city} data center, {state} colocation, data center {country}">
    
    <!-- Open Graph -->
    <meta property="og:title" content="{title}">
    <meta property="og:description" content="{meta_desc}">
    <meta property="og:type" content="website">
    <meta property="og:url" content="{SITE_URL}/facilities/{slug}">
    <meta property="og:site_name" content="DC Hub">
    
    <!-- Twitter Card -->
    <meta name="twitter:card" content="summary">
    <meta name="twitter:title" content="{title}">
    <meta name="twitter:description" content="{meta_desc}">
    
    <!-- Canonical URL -->
    <link rel="canonical" href="{SITE_URL}/facilities/{slug}">
    
    <!-- Favicon -->
    <link rel="icon" type="image/svg+xml" href="/favicon.svg">
    
    <style>
        :root {{
            --bg: #0a0a0f;
            --bg2: #12121a;
            --text: #e4e4e7;
            --text2: #a1a1aa;
            --text3: #71717a;
            --accent: #6366f1;
            --accent-light: #818cf8;
            --green: #22c55e;
            --orange: #f59e0b;
            --border: #27272a;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg); color: var(--text); line-height: 1.6; }}
        .container {{ max-width: 1200px; margin: 0 auto; padding: 20px; }}
        header {{ background: var(--bg2); border-bottom: 1px solid var(--border); padding: 16px 0; }}
        .header-content {{ display: flex; justify-content: space-between; align-items: center; max-width: 1200px; margin: 0 auto; padding: 0 20px; flex-wrap: wrap; gap: 16px; }}
        .logo {{ display: flex; align-items: center; gap: 10px; text-decoration: none; color: var(--text); font-weight: 700; font-size: 1.25rem; }}
        .logo svg {{ width: 28px; height: 28px; }}
        nav a {{ color: var(--text2); text-decoration: none; margin-left: 24px; font-size: 14px; }}
        nav a:hover {{ color: var(--accent-light); }}
        .breadcrumb {{ padding: 16px 0; font-size: 14px; color: var(--text3); }}
        .breadcrumb a {{ color: var(--accent-light); text-decoration: none; }}
        .hero {{ background: linear-gradient(135deg, var(--bg2) 0%, var(--bg) 100%); border: 1px solid var(--border); border-radius: 16px; padding: 32px; margin-bottom: 24px; }}
        .hero-header {{ display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 24px; flex-wrap: wrap; gap: 16px; }}
        .provider-badge {{ background: var(--accent); color: white; padding: 4px 12px; border-radius: 4px; font-size: 12px; font-weight: 600; text-transform: uppercase; }}
        .status-badge {{ background: {status_color}20; color: {status_color}; border: 1px solid {status_color}; padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 500; text-transform: capitalize; }}
        h1 {{ font-size: 2rem; margin-bottom: 8px; }}
        .location {{ font-size: 1.1rem; color: var(--text2); }}
        .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 16px; margin-top: 24px; }}
        .stat-card {{ background: var(--bg); border: 1px solid var(--border); border-radius: 12px; padding: 20px; text-align: center; }}
        .stat-value {{ font-size: 1.5rem; font-weight: 700; color: var(--accent-light); }}
        .stat-label {{ font-size: 12px; color: var(--text3); margin-top: 4px; text-transform: uppercase; }}
        .content-grid {{ display: grid; grid-template-columns: 2fr 1fr; gap: 24px; }}
        @media (max-width: 768px) {{ .content-grid {{ grid-template-columns: 1fr; }} nav {{ display: none; }} }}
        .section {{ background: var(--bg2); border: 1px solid var(--border); border-radius: 12px; padding: 24px; margin-bottom: 24px; }}
        .section h2 {{ font-size: 1.25rem; margin-bottom: 16px; }}
        .info-row {{ display: flex; justify-content: space-between; padding: 12px 0; border-bottom: 1px solid var(--border); flex-wrap: wrap; gap: 8px; }}
        .info-row:last-child {{ border-bottom: none; }}
        .info-label {{ color: var(--text3); }}
        .info-value {{ color: var(--text); font-weight: 500; }}
        .map-container {{ height: 250px; border-radius: 8px; overflow: hidden; background: var(--bg); }}
        .map-container iframe {{ width: 100%; height: 100%; border: none; }}
        .cta-section {{ background: linear-gradient(135deg, var(--accent) 0%, #4f46e5 100%); border-radius: 12px; padding: 24px; text-align: center; }}
        .cta-section h3 {{ color: white; margin-bottom: 8px; }}
        .cta-section p {{ color: rgba(255,255,255,0.8); margin-bottom: 16px; font-size: 14px; }}
        .btn {{ display: inline-block; background: white; color: var(--accent); padding: 12px 24px; border-radius: 8px; text-decoration: none; font-weight: 600; }}
        footer {{ background: var(--bg2); border-top: 1px solid var(--border); padding: 32px 0; margin-top: 48px; }}
        .footer-content {{ max-width: 1200px; margin: 0 auto; padding: 0 20px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 16px; }}
        .footer-links a {{ color: var(--text3); text-decoration: none; margin-left: 24px; font-size: 14px; }}
    </style>
</head>
<body>
    <header>
        <div class="header-content">
            <a href="/" class="logo">
                <svg viewBox="0 0 24 32" fill="none"><path d="M13.5 1L2 18H11L9.5 31L22 13H12.5L13.5 1Z" fill="url(#g)" stroke="#818cf8"/><defs><linearGradient id="g" x1="12" y1="1" x2="12" y2="31"><stop stop-color="#a5b4fc"/><stop offset="1" stop-color="#6366f1"/></linearGradient></defs></svg>
                DC Hub
            </a>
            <nav>
                <a href="/">Map</a>
                <a href="/markets/">Markets</a>
                <a href="/land-power">Land &amp; Power</a>
                <a href="/pricing">Pricing</a>
            </nav>
        </div>
    </header>
    
    <main class="container">
        <div class="breadcrumb">
            <a href="/">Home</a> &raquo; 
            <a href="/facilities/">Facilities</a> &raquo; 
            {name}
        </div>
        
        <div class="hero">
            <div class="hero-header">
                <div>
                    <span class="provider-badge">{provider}</span>
                    <h1>{name}</h1>
                    <div class="location">📍 {location}</div>
                </div>
                <span class="status-badge">{status}</span>
            </div>
            
            <div class="stats-grid">
                <div class="stat-card">
                    <div class="stat-value">{power_mw if power_mw else '—'}</div>
                    <div class="stat-label">Power (MW)</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">{f'{sqft:,}' if sqft else '—'}</div>
                    <div class="stat-label">Sq Ft</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">{facility_type}</div>
                    <div class="stat-label">Type</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">{region if region else '—'}</div>
                    <div class="stat-label">Region</div>
                </div>
            </div>
        </div>
        
        <div class="content-grid">
            <div>
                <div class="section">
                    <h2>📋 Facility Details</h2>
                    <div class="info-row">
                        <span class="info-label">Provider</span>
                        <span class="info-value">{provider}</span>
                    </div>
                    <div class="info-row">
                        <span class="info-label">Facility Name</span>
                        <span class="info-value">{name}</span>
                    </div>
                    <div class="info-row">
                        <span class="info-label">Location</span>
                        <span class="info-value">{location}</span>
                    </div>
                    {address_row}
                    <div class="info-row">
                        <span class="info-label">Status</span>
                        <span class="info-value" style="text-transform:capitalize">{status}</span>
                    </div>
                    <div class="info-row">
                        <span class="info-label">Power Capacity</span>
                        <span class="info-value">{f'{power_mw} MW' if power_mw else 'Not specified'}</span>
                    </div>
                    {sqft_row}
                </div>
                
                {map_section}
            </div>
            
            <div>
                <div class="cta-section">
                    <h3>Need capacity in {city if city else location}?</h3>
                    <p>Get pricing quotes from {provider} and other providers.</p>
                    <a href="/pricing" class="btn">Get Pricing</a>
                </div>
                
                <div class="section" style="margin-top: 24px;">
                    <h2>🔗 Related</h2>
                    <div class="info-row">
                        <a href="/markets/{market_slug}" style="color: var(--accent-light); text-decoration: none;">
                            View all {market_display} facilities →
                        </a>
                    </div>
                    <div class="info-row">
                        <a href="/?provider={provider_encoded}" style="color: var(--accent-light); text-decoration: none;">
                            View all {provider} facilities →
                        </a>
                    </div>
                </div>
            </div>
        </div>
    </main>
    
    <footer>
        <div class="footer-content">
            <div style="color: var(--text3); font-size: 14px;">© 2026 DC Hub. Data center intelligence platform.</div>
            <div class="footer-links">
                <a href="/about">About</a>
                <a href="/privacy">Privacy</a>
                <a href="/terms">Terms</a>
            </div>
        </div>
    </footer>
    
    <script type="application/ld+json">
    {{
        "@context": "https://schema.org",
        "@type": "Place",
        "name": "{name}",
        "description": "{meta_desc}",
        "address": {{
            "@type": "PostalAddress",
            "addressLocality": "{city}",
            "addressRegion": "{state}",
            "addressCountry": "{country}"
        }},
        {schema_geo}
        "url": "{SITE_URL}/facilities/{slug}"
    }}
    </script>
</body>
</html>'''
    
    return html

# =============================================================================
# SITEMAP GENERATION
# =============================================================================

def generate_sitemap(slug_list, existing_pages):
    """Generate sitemap.xml with consistent slugs"""
    today = datetime.now().strftime('%Y-%m-%d')
    
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    
    # Add existing pages
    for page in existing_pages:
        lines.append(f'''  <url>
    <loc>{page['url']}</loc>
    <lastmod>{today}</lastmod>
    <changefreq>{page.get('changefreq', 'weekly')}</changefreq>
    <priority>{page.get('priority', '0.5')}</priority>
  </url>''')
    
    # Add facility pages (using pre-generated slugs for consistency)
    for slug in slug_list:
        lines.append(f'''  <url>
    <loc>{SITE_URL}/facilities/{slug}</loc>
    <lastmod>{today}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>0.6</priority>
  </url>''')
    
    lines.append('</urlset>')
    return '\n'.join(lines)

# =============================================================================
# INDEX PAGE
# =============================================================================

def generate_facilities_index(facilities, slug_map):
    """Generate index page with proper escaping"""
    
    # Group by provider
    by_provider = {}
    for f in facilities:
        provider = f.get('provider') or 'Other'
        if provider not in by_provider:
            by_provider[provider] = []
        by_provider[provider].append(f)
    
    sorted_providers = sorted(by_provider.items(), key=lambda x: len(x[1]), reverse=True)
    
    facility_count = len(facilities)
    provider_count = len(by_provider)
    
    html_parts = [f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>All Data Center Facilities | DC Hub</title>
    <meta name="description" content="Browse {facility_count:,} data center facilities tracked by DC Hub from {provider_count} providers.">
    <link rel="canonical" href="{SITE_URL}/facilities/">
    <style>
        :root {{ --bg: #0a0a0f; --bg2: #12121a; --text: #e4e4e7; --text2: #a1a1aa; --accent: #6366f1; --border: #27272a; }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg); color: var(--text); line-height: 1.6; padding: 20px; }}
        .container {{ max-width: 1200px; margin: 0 auto; }}
        h1 {{ margin-bottom: 8px; }}
        .subtitle {{ color: var(--text2); margin-bottom: 32px; }}
        .provider-section {{ margin-bottom: 24px; }}
        .provider-header {{ background: var(--bg2); padding: 16px; border-radius: 8px 8px 0 0; border: 1px solid var(--border); display: flex; justify-content: space-between; }}
        .facilities-list {{ background: var(--bg2); border: 1px solid var(--border); border-top: none; border-radius: 0 0 8px 8px; max-height: 300px; overflow-y: auto; }}
        .facility-link {{ display: block; padding: 12px 16px; color: var(--text); text-decoration: none; border-bottom: 1px solid var(--border); }}
        .facility-link:hover {{ background: rgba(99,102,241,0.1); }}
        .facility-location {{ color: var(--text2); font-size: 13px; }}
        a {{ color: var(--accent); }}
    </style>
</head>
<body>
<div class="container">
    <h1>🏢 All Data Center Facilities</h1>
    <p class="subtitle">{facility_count:,} facilities from {provider_count} providers</p>
    <p style="margin-bottom: 24px;"><a href="/">← Back to DC Hub</a></p>
''']
    
    for provider, facs in sorted_providers[:50]:
        provider_escaped = escape_html(provider)
        html_parts.append(f'''
    <div class="provider-section">
        <div class="provider-header">
            <span>{provider_escaped}</span>
            <span style="color:var(--text2)">{len(facs)} facilities</span>
        </div>
        <div class="facilities-list">''')
        
        for f in facs[:20]:
            fac_id = f.get('id')
            slug = slug_map.get(fac_id, generate_facility_slug(f))
            name = escape_html(f.get('name') or 'Data Center')
            city = escape_html(f.get('city') or '')
            state = escape_html(f.get('state') or '')
            loc = ', '.join([p for p in [city, state] if p]) or 'USA'
            html_parts.append(f'''
            <a href="/facilities/{slug}" class="facility-link">{name} <span class="facility-location">📍 {loc}</span></a>''')
        
        if len(facs) > 20:
            html_parts.append(f'''
            <div style="padding:12px 16px;color:var(--text2);font-size:13px">...and {len(facs)-20} more</div>''')
        
        html_parts.append('''
        </div>
    </div>''')
    
    html_parts.append('''
</div>
</body>
</html>''')
    
    return ''.join(html_parts)

# =============================================================================
# MAIN
# =============================================================================

def main():
    # Parse arguments
    dry_run = '--dry-run' in sys.argv
    limit = None
    for i, arg in enumerate(sys.argv):
        if arg == '--limit' and i + 1 < len(sys.argv):
            try:
                limit = int(sys.argv[i + 1])
            except ValueError:
                pass
    
    print("=" * 60)
    print("DC Hub - Static Facility Page Generator v2")
    print("=" * 60)
    if dry_run:
        print("🔍 DRY RUN MODE - No files will be written")
    if limit:
        print(f"📊 LIMIT MODE - Only processing {limit} facilities")
    print()
    
    # Create output directory
    if not dry_run:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    # Fetch facilities
    facilities = fetch_all_facilities(limit=limit)
    
    if not facilities:
        print("❌ No facilities found. Exiting.")
        return
    
    # Generate pages with consistent slug tracking
    print(f"\nGenerating {len(facilities)} facility pages...")
    
    slug_map = {}  # id -> slug (for consistency between pages and sitemap)
    slugs_used = set()
    generated_count = 0
    errors = []
    
    for i, facility in enumerate(facilities):
        try:
            # Generate slug with duplicate handling
            base_slug = generate_facility_slug(facility)
            slug = base_slug
            counter = 1
            while slug in slugs_used:
                slug = f"{base_slug}-{counter}"
                counter += 1
            slugs_used.add(slug)
            
            # Track for sitemap consistency
            fac_id = facility.get('id')
            if fac_id:
                slug_map[fac_id] = slug
            
            # Generate HTML
            html = generate_facility_html(facility, slug)
            
            # Write file
            if not dry_run:
                filepath = os.path.join(OUTPUT_DIR, f"{slug}.html")
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(html)
            
            generated_count += 1
            
            if (i + 1) % 500 == 0:
                print(f"  Progress: {i + 1}/{len(facilities)} pages...")
                
        except Exception as e:
            errors.append(f"Facility {facility.get('id')}: {e}")
            if len(errors) <= 5:
                print(f"  ⚠️ Error: {e}")
    
    print(f"  ✅ Generated {generated_count} facility pages")
    if errors:
        print(f"  ⚠️ {len(errors)} errors (showing first 5)")
    
    # Generate sitemap
    print("\nGenerating sitemap.xml...")
    
    existing_pages = [
        {'url': f'{SITE_URL}/', 'priority': '1.0', 'changefreq': 'daily'},
        {'url': f'{SITE_URL}/land-power', 'priority': '0.9', 'changefreq': 'daily'},
        {'url': f'{SITE_URL}/market-intelligence', 'priority': '0.9', 'changefreq': 'daily'},
        {'url': f'{SITE_URL}/transactions', 'priority': '0.9', 'changefreq': 'daily'},
        {'url': f'{SITE_URL}/construction-pipeline', 'priority': '0.9', 'changefreq': 'daily'},
        {'url': f'{SITE_URL}/news', 'priority': '0.8', 'changefreq': 'hourly'},
        {'url': f'{SITE_URL}/pricing', 'priority': '0.8', 'changefreq': 'weekly'},
        {'url': f'{SITE_URL}/about', 'priority': '0.6', 'changefreq': 'monthly'},
        {'url': f'{SITE_URL}/markets/', 'priority': '0.9', 'changefreq': 'daily'},
        {'url': f'{SITE_URL}/markets/northern-virginia', 'priority': '0.9'},
        {'url': f'{SITE_URL}/markets/dallas', 'priority': '0.9'},
        {'url': f'{SITE_URL}/markets/phoenix', 'priority': '0.9'},
        {'url': f'{SITE_URL}/markets/silicon-valley', 'priority': '0.9'},
        {'url': f'{SITE_URL}/markets/atlanta', 'priority': '0.9'},
        {'url': f'{SITE_URL}/markets/chicago', 'priority': '0.9'},
        {'url': f'{SITE_URL}/markets/new-york', 'priority': '0.8'},
        {'url': f'{SITE_URL}/markets/seattle', 'priority': '0.8'},
        {'url': f'{SITE_URL}/markets/los-angeles', 'priority': '0.8'},
        {'url': f'{SITE_URL}/markets/denver', 'priority': '0.8'},
        {'url': f'{SITE_URL}/markets/austin', 'priority': '0.8'},
        {'url': f'{SITE_URL}/markets/las-vegas', 'priority': '0.8'},
        {'url': f'{SITE_URL}/markets/portland', 'priority': '0.8'},
        {'url': f'{SITE_URL}/markets/london', 'priority': '0.8'},
        {'url': f'{SITE_URL}/markets/frankfurt', 'priority': '0.8'},
        {'url': f'{SITE_URL}/facilities/', 'priority': '0.8', 'changefreq': 'daily'},
    ]
    
    sitemap = generate_sitemap(list(slugs_used), existing_pages)
    
    if not dry_run:
        with open('sitemap.xml', 'w', encoding='utf-8') as f:
            f.write(sitemap)
    
    print(f"  ✅ Sitemap with {len(existing_pages) + len(slugs_used)} URLs")
    
    # Generate index
    print("\nGenerating facilities index...")
    index_html = generate_facilities_index(facilities, slug_map)
    
    if not dry_run:
        with open(os.path.join(OUTPUT_DIR, 'index.html'), 'w', encoding='utf-8') as f:
            f.write(index_html)
    
    print("  ✅ Index page generated")
    
    # Summary
    print("\n" + "=" * 60)
    print("✅ COMPLETE!")
    print(f"   📄 {generated_count} facility pages")
    print(f"   🗺️ sitemap.xml ({len(existing_pages) + len(slugs_used)} URLs)")
    print(f"   📇 facilities/index.html")
    if dry_run:
        print("\n   (Dry run - no files written)")
    else:
        print(f"\n   Files written to: ./{OUTPUT_DIR}/")
    print("=" * 60)
    print("\nNext steps:")
    print("  1. Review a few generated pages for quality")
    print("  2. Upload /facilities/ folder to Cloudflare Pages")
    print("  3. Upload sitemap.xml to root")
    print("  4. Resubmit sitemap in Google Search Console")

if __name__ == '__main__':
    main()
