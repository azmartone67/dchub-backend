"""
DC Hub SEO Agent - AI-Powered Search Engine Optimization
=========================================================
Agents that help accelerate Google indexing and build backlinks:
1. IndexNow Agent - Ping search engines for instant indexing
2. Backlink Agent - Generate linkable content and track opportunities
3. Citation Agent - Monitor AI platform citations as "new backlinks"
"""

from flask import Blueprint, request, jsonify
from datetime import datetime, timedelta
import json
import os
import requests
import hashlib
from db_utils import get_db

seo_agent_bp = Blueprint('seo_agent', __name__)

DB_PATH = os.environ.get('DB_PATH', 'dc_nexus.db')
INDEXNOW_KEY = os.environ.get('INDEXNOW_KEY', '')

def init_seo_tables():
    """Initialize SEO tracking tables"""
    conn = get_db()
    try:
        c = conn.cursor()

        c.execute('''CREATE TABLE IF NOT EXISTS seo_indexing_log (
            id SERIAL PRIMARY KEY,
            url TEXT NOT NULL,
            search_engine TEXT NOT NULL,
            status TEXT,
            response_code INTEGER,
            submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS seo_backlinks (
            id SERIAL PRIMARY KEY,
            source_url TEXT NOT NULL,
            target_url TEXT,
            anchor_text TEXT,
            domain_authority INTEGER,
            status TEXT DEFAULT 'discovered',
            discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            verified_at TIMESTAMP
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS seo_content_opportunities (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            content_type TEXT,
            target_keywords TEXT,
            priority INTEGER DEFAULT 5,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS ai_citations (
            id SERIAL PRIMARY KEY,
            platform TEXT NOT NULL,
            query TEXT,
            cited_url TEXT,
            citation_type TEXT,
            detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        conn.commit()
    finally:
        conn.close()
    print("✅ SEO Agent tables initialized")

def ping_indexnow(urls):
    """Submit URLs to IndexNow for rapid indexing"""
    if not INDEXNOW_KEY:
        return {"error": "INDEXNOW_KEY not configured", "success": False}
    
    results = []
    
    engines = [
        {"name": "Bing/IndexNow", "endpoint": "https://www.bing.com/indexnow"},
        {"name": "Yandex", "endpoint": "https://yandex.com/indexnow"},
        {"name": "Seznam", "endpoint": "https://search.seznam.cz/indexnow"},
        {"name": "Naver", "endpoint": "https://searchadvisor.naver.com/indexnow"}
    ]
    
    host = "dchub.cloud"
    key_location = f"https://{host}/{INDEXNOW_KEY}.txt"
    
    for engine in engines:
        try:
            if len(urls) == 1:
                params = {
                    "url": urls[0],
                    "key": INDEXNOW_KEY
                }
                response = requests.get(engine["endpoint"], params=params, timeout=10)
            else:
                payload = {
                    "host": host,
                    "key": INDEXNOW_KEY,
                    "keyLocation": key_location,
                    "urlList": urls[:10000]
                }
                response = requests.post(
                    engine["endpoint"],
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=10
                )
            
            status = "success" if response.status_code in [200, 202] else "failed"
            results.append({
                "engine": engine["name"],
                "status": status,
                "code": response.status_code
            })
            
            conn = get_db()
            try:
                c = conn.cursor()
                for url in urls[:100]:
                    c.execute('''INSERT INTO seo_indexing_log
                        (url, search_engine, status, response_code)
                        VALUES (%s, %s, %s, %s) ON CONFLICT (url) DO UPDATE SET search_engine = EXCLUDED.search_engine, status = EXCLUDED.status, response_code = EXCLUDED.response_code''',
                        (url, engine["name"], status, response.status_code))
                conn.commit()
            finally:
                conn.close()
            
        except Exception as e:
            results.append({
                "engine": engine["name"],
                "status": "error",
                "error": str(e)
            })
    
    return {
        "success": True,
        "urls_submitted": len(urls),
        "engines": results
    }

def get_priority_urls():
    """Get high-priority URLs for indexing"""
    base_url = "https://dchub.cloud"
    
    priority_urls = [
        f"{base_url}/",
        f"{base_url}/api",
        f"{base_url}/markets",
        f"{base_url}/facilities",
        f"{base_url}/transactions",
        f"{base_url}/news",
        f"{base_url}/llms.txt",
        f"{base_url}/ai.txt",
        f"{base_url}/AGENTS.md",
        f"{base_url}/.well-known/ai-plugin.json"
    ]
    
    try:
        conn = get_db()
        try:
            c = conn.cursor()

            c.execute('''SELECT DISTINCT city, state, country FROM facilities
                WHERE city IS NOT NULL LIMIT 50''')
            markets = c.fetchall()

            for city, state, country in markets:
                slug = city.lower().replace(' ', '-') if city else ''
                if slug:
                    priority_urls.append(f"{base_url}/market/{slug}")

            c.execute('''SELECT id FROM facilities ORDER BY updated_at DESC LIMIT 100''')
            facilities = c.fetchall()
            for (fid,) in facilities:
                priority_urls.append(f"{base_url}/facility/{fid}")

        finally:
            conn.close()
    except Exception as e:
        print(f"Error getting priority URLs: {e}")
    
    return priority_urls

def generate_seo_content_ideas():
    """Generate AI-powered content ideas for backlinks"""
    ideas = []
    
    try:
        conn = get_db()
        try:
            c = conn.cursor()

            c.execute('''SELECT city, COUNT(*) as cnt FROM facilities
                WHERE city IS NOT NULL
                GROUP BY city ORDER BY cnt DESC LIMIT 10''')
            top_markets = c.fetchall()

            for city, count in top_markets:
                ideas.append({
                    "title": f"Data Center Market Report: {city}",
                    "type": "market_report",
                    "keywords": f"{city} data center, {city} colocation, {city} cloud",
                    "priority": 9,
                    "backlink_potential": "high"
                })

            c.execute('''SELECT headline, source FROM announcements
                ORDER BY published_at DESC LIMIT 5''')
            news = c.fetchall()

            for headline, source in news:
                ideas.append({
                    "title": f"Analysis: {headline[:50]}...",
                    "type": "news_analysis",
                    "keywords": "data center news, industry analysis",
                    "priority": 7,
                    "backlink_potential": "medium"
                })

        finally:
            conn.close()
    except Exception as e:
        print(f"Error generating content ideas: {e}")
    
    ideas.extend([
        {
            "title": "2026 Data Center Market Trends Report",
            "type": "annual_report",
            "keywords": "data center trends 2026, market forecast",
            "priority": 10,
            "backlink_potential": "very_high"
        },
        {
            "title": "Data Center Site Selection Guide",
            "type": "guide",
            "keywords": "site selection, data center location",
            "priority": 9,
            "backlink_potential": "high"
        },
        {
            "title": "Power Infrastructure for Data Centers",
            "type": "educational",
            "keywords": "data center power, electrical infrastructure",
            "priority": 8,
            "backlink_potential": "high"
        }
    ])
    
    return ideas

def get_ai_citation_stats():
    """Track AI platforms citing DC Hub as 'new backlinks'"""
    try:
        conn = get_db()
        try:
            c = conn.cursor()

            c.execute('''SELECT platform, COUNT(*) as citations,
                MAX(timestamp) as last_citation
                FROM ai_usage_tracking
                GROUP BY platform
                ORDER BY citations DESC''')

            citations = []
            for platform, count, last in c.fetchall():
                citations.append({
                    "platform": platform,
                    "citations": count,
                    "last_cited": last,
                    "seo_value": "high" if count > 5 else "medium"
                })

            c.execute("SELECT COUNT(*) FROM ai_usage_tracking")
            total = c.fetchone()[0] or 0

        finally:
            conn.close()
        
        return {
            "total_ai_citations": total,
            "platforms_citing": len(citations),
            "citations_by_platform": citations,
            "seo_impact": "AI citations are the new backlinks - builds authority with Google"
        }
    except Exception as e:
        return {"error": str(e), "total_ai_citations": 0}


@seo_agent_bp.route('/api/seo/status', methods=['GET'])
def seo_status():
    """Get SEO agent status and metrics"""
    try:
        conn = get_db()
        try:
            c = conn.cursor()

            c.execute("SELECT COUNT(*) FROM seo_indexing_log WHERE status = 'success'")
            indexed = c.fetchone()[0] or 0

            c.execute("SELECT COUNT(*) FROM seo_backlinks")
            backlinks = c.fetchone()[0] or 0

            c.execute('''SELECT COUNT(*) FROM seo_indexing_log
                WHERE submitted_at > datetime('now', '-24 hours')''')
            today_pings = c.fetchone()[0] or 0

        finally:
            conn.close()
        
        return jsonify({
            "success": True,
            "status": "active",
            "agents": {
                "indexnow": {"status": "active" if INDEXNOW_KEY else "needs_key"},
                "backlink": {"status": "active"},
                "citation": {"status": "active"}
            },
            "metrics": {
                "urls_indexed": indexed,
                "backlinks_tracked": backlinks,
                "indexnow_pings_24h": today_pings,
                "ai_citations": get_ai_citation_stats()
            },
            "indexnow_configured": bool(INDEXNOW_KEY)
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@seo_agent_bp.route('/api/seo/indexnow/ping', methods=['POST'])
def indexnow_ping():
    """Ping IndexNow to request rapid indexing"""
    data = request.get_json() or {}
    urls = data.get('urls', [])
    
    if not urls:
        urls = get_priority_urls()
    
    result = ping_indexnow(urls)
    return jsonify(result)


@seo_agent_bp.route('/api/seo/indexnow/ping-all', methods=['POST'])
def indexnow_ping_all():
    """Ping all priority URLs for indexing"""
    urls = get_priority_urls()
    result = ping_indexnow(urls)
    return jsonify({
        **result,
        "priority_urls": len(urls)
    })


@seo_agent_bp.route('/api/seo/content-ideas', methods=['GET'])
def content_ideas():
    """Get AI-generated content ideas for backlinks"""
    ideas = generate_seo_content_ideas()
    return jsonify({
        "success": True,
        "content_ideas": ideas,
        "strategy": "Create linkable assets that attract backlinks naturally"
    })


@seo_agent_bp.route('/api/seo/ai-citations', methods=['GET'])
def ai_citations():
    """Track AI citations as modern backlinks"""
    stats = get_ai_citation_stats()
    return jsonify({
        "success": True,
        **stats,
        "insight": "Each AI platform citing DC Hub builds domain authority"
    })


@seo_agent_bp.route('/api/seo/backlink-opportunities', methods=['GET'])
def backlink_opportunities():
    """Get potential backlink opportunities"""
    opportunities = [
        {
            "type": "directory",
            "name": "Data Center Knowledge Directory",
            "domain_authority": 65,
            "status": "target",
            "action": "Submit listing"
        },
        {
            "type": "guest_post",
            "name": "Data Center Frontier",
            "domain_authority": 55,
            "status": "target",
            "action": "Pitch article on market trends"
        },
        {
            "type": "resource_page",
            "name": "University research pages",
            "domain_authority": 70,
            "status": "target",
            "action": "Suggest DC Hub as data source"
        },
        {
            "type": "ai_platform",
            "name": "OpenAI GPT Store",
            "domain_authority": 95,
            "status": "integrated",
            "action": "Publish as GPT action"
        },
        {
            "type": "ai_platform",
            "name": "Anthropic MCP",
            "domain_authority": 90,
            "status": "integrated",
            "action": "Register as MCP server"
        }
    ]
    
    return jsonify({
        "success": True,
        "opportunities": opportunities,
        "total": len(opportunities),
        "strategy": "AI platform integrations = high-authority backlinks"
    })


@seo_agent_bp.route('/api/seo/run-cycle', methods=['POST'])
def run_seo_cycle():
    """Run a full SEO optimization cycle"""
    results = {
        "timestamp": datetime.now().isoformat(),
        "actions": []
    }
    
    urls = get_priority_urls()
    indexnow_result = ping_indexnow(urls[:50])
    results["actions"].append({
        "action": "indexnow_ping",
        "urls_submitted": len(urls[:50]),
        "result": indexnow_result
    })
    
    content_ideas = generate_seo_content_ideas()
    results["actions"].append({
        "action": "content_ideas_generated",
        "count": len(content_ideas)
    })
    
    ai_stats = get_ai_citation_stats()
    results["actions"].append({
        "action": "ai_citations_tracked",
        "total": ai_stats.get("total_ai_citations", 0)
    })
    
    results["success"] = True
    results["summary"] = f"SEO cycle complete: {len(urls[:50])} URLs pinged, {len(content_ideas)} content ideas, {ai_stats.get('total_ai_citations', 0)} AI citations"
    
    return jsonify(results)


def register_seo_agent(app):
    """Register SEO agent with Flask app"""
    init_seo_tables()
    app.register_blueprint(seo_agent_bp)
    print("🔍 SEO Agent registered:")
    print("   GET  /api/seo/status - SEO metrics")
    print("   POST /api/seo/indexnow/ping - Ping search engines")
    print("   POST /api/seo/indexnow/ping-all - Ping all priority URLs")
    print("   GET  /api/seo/content-ideas - Backlink content ideas")
    print("   GET  /api/seo/ai-citations - AI citation tracking")
    print("   GET  /api/seo/backlink-opportunities - Link opportunities")
    print("   POST /api/seo/run-cycle - Run full SEO cycle")
