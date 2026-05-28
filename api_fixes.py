"""
DC HUB API FIXES - v49
======================
Add these fixes to your Replit api_server.py

ISSUES FIXED:
1. Search not filtering by location (Phoenix returns Ashburn!)
2. News endpoint missing (404)
3. Railway contamination in database
"""

# =====================================================
# FIX 1: IMPROVED FACILITY SEARCH
# Replace your existing search/facilities endpoint with this
# =====================================================

from flask import Flask, request, jsonify
import sqlite3
import re
from html import unescape
from db_utils import get_db

# HTML stripping helper
def strip_html(text):
    """Remove HTML tags from text"""
    if not text:
        return ""
    text = unescape(text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

# Market aliases for better search
MARKET_ALIASES = {
    'phoenix': ['Phoenix', 'Mesa', 'Tempe', 'Scottsdale', 'Chandler', 'Gilbert', 'Goodyear', 'AZ'],
    'dallas': ['Dallas', 'Fort Worth', 'Plano', 'Irving', 'Arlington', 'Carrollton', 'Richardson', 'TX'],
    'northern virginia': ['Ashburn', 'Loudoun', 'Sterling', 'Reston', 'Herndon', 'Manassas', 'VA'],
    'nova': ['Ashburn', 'Loudoun', 'Sterling', 'Reston', 'Herndon', 'Manassas', 'VA'],
    'ashburn': ['Ashburn', 'Loudoun', 'VA'],
    'chicago': ['Chicago', 'Aurora', 'Elk Grove', 'Schaumburg', 'IL'],
    'atlanta': ['Atlanta', 'Marietta', 'Alpharetta', 'Duluth', 'GA'],
    'silicon valley': ['San Jose', 'Santa Clara', 'Sunnyvale', 'Milpitas', 'Fremont', 'CA'],
    'los angeles': ['Los Angeles', 'El Segundo', 'Downtown LA', 'CA'],
    'new york': ['New York', 'NYC', 'Manhattan', 'Secaucus', 'NJ', 'NY'],
    'seattle': ['Seattle', 'Tukwila', 'Kent', 'WA'],
    'denver': ['Denver', 'Aurora', 'Centennial', 'CO'],
    'austin': ['Austin', 'Round Rock', 'TX'],
    'houston': ['Houston', 'TX'],
    'miami': ['Miami', 'Boca Raton', 'FL'],
    'columbus': ['Columbus', 'New Albany', 'Dublin', 'OH'],
    'salt lake': ['Salt Lake City', 'West Valley', 'UT'],
    'portland': ['Portland', 'Hillsboro', 'OR'],
    'reno': ['Reno', 'Sparks', 'NV'],
    'las vegas': ['Las Vegas', 'Henderson', 'NV'],
}

# AUTO-REPAIR: duplicate route '/api/v1/facilities' also in main.py:13541 — review and remove one
@app.route('/api/v1/facilities')
def get_facilities():
    """
    Get facilities with PROPER location-based search
    """
    q = request.args.get('q', '').strip().lower()
    limit = min(int(request.args.get('limit', 100)), 500)
    offset = int(request.args.get('offset', 0))
    region = request.args.get('region', '')
    status = request.args.get('status', '')
    
    conn = get_db()
    # sqlite3.Row removed - PostgreSQL uses RealDictCursor or dict(row)
    cursor = conn.cursor()
    
    # Build query
    conditions = []
    params = []
    
    # Exclude railway contamination
    conditions.append("""
        provider NOT LIKE '%Railway%' 
        AND provider NOT LIKE '%Railroad%' 
        AND provider NOT LIKE '%SNCF%'
        AND provider NOT LIKE '%NMBS%'
        AND provider NOT LIKE '%Station&Service%'
        AND provider NOT LIKE '%chemins de fer%'
    """)
    
    if q:
        # Check if it's a known market alias
        search_terms = MARKET_ALIASES.get(q, [q])
        
        # Build OR conditions for all aliases
        alias_conditions = []
        for term in search_terms:
            alias_conditions.append("(city LIKE %s OR state LIKE %s OR name LIKE %s OR provider LIKE %s OR address LIKE %s)")
            params.extend([f'%{term}%'] * 5)
        
        conditions.append(f"({' OR '.join(alias_conditions)})")
    
    if region:
        conditions.append("region = %s")
        params.append(region)
    
    if status:
        conditions.append("status = %s")
        params.append(status)
    
    where_clause = " AND ".join(conditions) if conditions else "1=1"
    
    # Get total count
    count_query = f"SELECT COUNT(*) FROM facilities WHERE {where_clause}"
    cursor.execute(count_query, params)
    total = cursor.fetchone()[0]
    
    # Get facilities
    query = f"""
        SELECT * FROM facilities 
        WHERE {where_clause}
        ORDER BY 
            CASE WHEN city LIKE ? THEN 0 ELSE 1 END,
            power_mw DESC,
            name ASC
        LIMIT %s OFFSET %s
    """
    
    # Add the search term for sorting priority
    sort_term = f'%{q}%' if q else '%'
    cursor.execute(query, params + [sort_term, limit, offset])
    
    facilities = []
    for row in cursor.fetchall():
        facilities.append(dict(row))
    
    conn.close()
    
    return jsonify({
        'success': True,
        'data': facilities,
        'total': total,
        'limit': limit,
        'offset': offset,
        'query': q
    })


# =====================================================
# FIX 2: ADD NEWS ENDPOINT
# Add this route to serve news articles
# =====================================================

@app.route('/api/v1/news')
def get_news():
    """
    Get news articles with HTML stripped
    """
    limit = min(int(request.args.get('limit', 50)), 100)
    offset = int(request.args.get('offset', 0))
    category = request.args.get('category', '')
    source = request.args.get('source', '')
    
    conn = get_db()
    # sqlite3.Row removed - PostgreSQL uses RealDictCursor or dict(row)
    cursor = conn.cursor()
    
    # Check if news_articles table exists
    cursor.execute("""
        SELECT name FROM sqlite_master 
        WHERE type='table' AND name='news_articles'
    """)
    if not cursor.fetchone():
        conn.close()
        return jsonify({
            'success': False,
            'error': 'News table not found. Run news_aggregator.py first.',
            'data': []
        })
    
    conditions = []
    params = []
    
    if category:
        conditions.append("categories LIKE %s")
        params.append(f'%{category}%')
    
    if source:
        conditions.append("source LIKE %s")
        params.append(f'%{source}%')
    
    where_clause = " AND ".join(conditions) if conditions else "1=1"
    
    # Get total
    cursor.execute(f"SELECT COUNT(*) FROM news_articles WHERE {where_clause}", params)
    total = cursor.fetchone()[0]
    
    # Get articles
    query = f"""
        SELECT * FROM news_articles 
        WHERE {where_clause}
        ORDER BY published_date DESC, id DESC
        LIMIT %s OFFSET %s
    """
    cursor.execute(query, params + [limit, offset])
    
    articles = []
    for row in cursor.fetchall():
        article = dict(row)
        # Strip HTML from title and description
        article['title'] = strip_html(article.get('title', ''))
        article['description'] = strip_html(article.get('description', ''))
        articles.append(article)
    
    conn.close()
    
    return jsonify({
        'success': True,
        'data': articles,
        'total': total,
        'limit': limit,
        'offset': offset
    })


@app.route('/api/v1/news/sources')
def get_news_sources():
    """Get list of news sources"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT source, COUNT(*) as count 
        FROM news_articles 
        GROUP BY source 
        ORDER BY count DESC
    """)
    
    sources = [{'source': row[0], 'count': row[1]} for row in cursor.fetchall()]
    conn.close()
    
    return jsonify({
        'success': True,
        'data': sources
    })


@app.route('/api/v1/news/sync', methods=['POST'])
def sync_news():
    """Trigger news sync (run news aggregator)"""
    import subprocess
    try:
        result = subprocess.run(['python', 'news_aggregator.py'], capture_output=True, text=True, timeout=120)
        return jsonify({
            'success': True,
            'message': 'News sync completed',
            'output': result.stdout
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        })


# =====================================================
# FIX 3: DATABASE CLEANUP - REMOVE RAILWAY CONTAMINATION
# Run this once to clean existing data
# =====================================================

def cleanup_railway_contamination():
    """Remove railway/train station entries from database"""
    conn = get_db()
    cursor = conn.cursor()
    
    # Count before
    cursor.execute("SELECT COUNT(*) FROM facilities")
    before_count = cursor.fetchone()[0]
    
    # Delete railway entries
    cursor.execute("""
        DELETE FROM facilities 
        WHERE provider LIKE '%Railway%' 
           OR provider LIKE '%Railroad%' 
           OR provider LIKE '%SNCF%'
           OR provider LIKE '%NMBS%'
           OR provider LIKE '%Station&Service%'
           OR provider LIKE '%chemins de fer%'
           OR provider LIKE '%Amtrak%'
           OR provider LIKE '%Metro%'
           OR provider LIKE '%Transit%'
           OR name LIKE '%Station%Railway%'
           OR name LIKE '%Train Station%'
    """)
    
    deleted = cursor.rowcount
    
    # Count after
    cursor.execute("SELECT COUNT(*) FROM facilities")
    after_count = cursor.fetchone()[0]
    
    conn.commit()
    conn.close()
    
    print(f"Railway Cleanup Complete:")
    print(f"  Before: {before_count} facilities")
    print(f"  Deleted: {deleted} railway entries")
    print(f"  After: {after_count} facilities")
    
    return deleted


# =====================================================
# FIX 4: IMPROVED STATS ENDPOINT (excludes railways)
# =====================================================
# AUTO-REPAIR: duplicate route '/api/v1/stats' also in main.py:13038 — review and remove one

@app.route('/api/v1/stats')
def get_stats():
    """Get platform statistics (excluding railway contamination)"""
    conn = get_db()
    cursor = conn.cursor()
    
    # Exclusion clause
    exclude = """
        provider NOT LIKE '%Railway%' 
        AND provider NOT LIKE '%Railroad%' 
        AND provider NOT LIKE '%SNCF%'
        AND provider NOT LIKE '%NMBS%'
        AND provider NOT LIKE '%Station&Service%'
        AND provider NOT LIKE '%chemins de fer%'
    """
    
    # Total facilities (excluding railways)
    cursor.execute(f"SELECT COUNT(*) FROM facilities WHERE {exclude}")
    total_facilities = cursor.fetchone()[0]
    
    # Total power
    cursor.execute(f"SELECT SUM(power_mw) FROM facilities WHERE {exclude} AND power_mw > 0")
    total_power = cursor.fetchone()[0] or 0
    
    # By status
    cursor.execute(f"""
        SELECT status, COUNT(*) 
        FROM facilities 
        WHERE {exclude}
        GROUP BY status
    """)
    by_status = {row[0]: row[1] for row in cursor.fetchall()}
    
    # By source (excluding railways)
    cursor.execute(f"""
        SELECT source, COUNT(*) 
        FROM facilities 
        WHERE {exclude}
        GROUP BY source
        ORDER BY COUNT(*) DESC
    """)
    by_source = {row[0]: row[1] for row in cursor.fetchall()}
    
    # Top countries
    cursor.execute(f"""
        SELECT country, COUNT(*) as cnt 
        FROM facilities 
        WHERE {exclude}
        GROUP BY country 
        ORDER BY cnt DESC 
        LIMIT 10
    """)
    top_countries = {row[0]: row[1] for row in cursor.fetchall()}
    
    # Top providers (REAL data center providers only)
    cursor.execute(f"""
        SELECT provider, COUNT(*) as cnt 
        FROM facilities 
        WHERE {exclude} AND provider IS NOT NULL AND provider != ''
        GROUP BY provider 
        ORDER BY cnt DESC 
        LIMIT 10
    """)
    top_providers = {row[0]: row[1] for row in cursor.fetchall()}
    
    # News count
    cursor.execute("SELECT COUNT(*) FROM news_articles")
    news_count = cursor.fetchone()[0] if cursor.fetchone() else 0
    
    conn.close()
    
    return jsonify({
        'success': True,
        'data': {
            'total_facilities': total_facilities,
            'total_power_mw': int(total_power),
            'total_announcements': news_count,
            'by_status': by_status,
            'by_source': by_source,
            'top_countries': top_countries,
            'top_providers': top_providers,
            'new_last_7_days': total_facilities  # Update with actual logic if needed
        },
        'generated_at': datetime.utcnow().isoformat()
    })


# =====================================================
# QUICK INSTALL INSTRUCTIONS FOR REPLIT
# =====================================================
"""
1. Open your api_server.py in Replit

2. Add the strip_html() function at the top

3. Add the MARKET_ALIASES dictionary

4. Replace your /api/v1/facilities route with the improved version

5. Add the /api/v1/news routes (get_news, get_news_sources, sync_news)

6. Update your /api/v1/stats route to exclude railways

7. Run the cleanup function once:
   >>> from api_server import cleanup_railway_contamination
   >>> cleanup_railway_contamination()

8. Restart the server

9. Test:
   - /api/v1/facilities?q=phoenix → Should return Phoenix facilities
   - /api/v1/news → Should return news articles without HTML
   - /api/v1/stats → Should exclude railway companies
"""

if __name__ == '__main__':
    # Run cleanup if executed directly
    print("Running railway contamination cleanup...")
    cleanup_railway_contamination()
