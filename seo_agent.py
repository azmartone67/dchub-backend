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
from routes._swallowed_writes import note_swallowed_write

seo_agent_bp = Blueprint('seo_agent', __name__)

DB_PATH = os.environ.get('DB_PATH', 'dc_nexus.db')
# r-indexnow-consolidate (2026-07-03): submission now delegates to
# routes.indexnow (DCHUB_INDEXNOW_KEY + committed key-file default), so this
# module is always "configured" regardless of the legacy INDEXNOW_KEY env var.
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
    """Submit URLs to IndexNow for rapid indexing.

    r-indexnow-consolidate (2026-07-03): delegates to the canonical
    routes.indexnow.submit_to_indexnow. This module's own engine loop was
    keyed on the INDEXNOW_KEY env var (unset in prod → every call returned
    "not configured" and submitted NOTHING), while the canonical path uses
    DCHUB_INDEXNOW_KEY with the committed key-file default and a persisted
    last-submit ledger. One submit path, one key. Per the IndexNow protocol
    a 2xx from ONE participating engine propagates to all of them, so the
    old 4-engine fan-out added no reach — only 4x the timeout exposure.
    Keeps this module's seo_indexing_log bookkeeping + return shape."""
    from routes.indexnow import submit_to_indexnow
    res = submit_to_indexnow(urls)
    ok = bool(res.get("ok"))
    status = "success" if ok else "failed"
    try:
        conn = get_db()
        try:
            c = conn.cursor()
            # Append one row per submitted URL. This used to upsert on url, but
            # the table has no unique constraint on url (see init_seo_tables),
            # so Postgres rejected every write with InvalidColumnReference and
            # note_swallowed_write ate it: logged on Railway 2026-09-11 at
            # 15:50:30Z and 17:36:20Z. An untargeted DO NOTHING needs no
            # constraint, and an append log is what /api/seo/status counts
            # (rows in the last 24h).
            for url in (urls or [])[:100]:
                c.execute('''INSERT INTO seo_indexing_log
                    (url, search_engine, status, response_code)
                    VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING''',
                    (url, res.get("endpoint") or "indexnow", status, res.get("status")))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        note_swallowed_write("seo_indexing_log", where="seo_agent.ping_indexnow")
        pass
    return {
        "success": ok,
        "urls_submitted": res.get("submitted", 0),
        "engines": [res],
    }

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

            c.execute("SELECT COUNT(*) FROM seo_indexing_log "
                      "WHERE submitted_at::timestamptz > NOW() - INTERVAL '24 hours'")
            today_pings = c.fetchone()[0] or 0

        finally:
            conn.close()
        
        return jsonify({
            "success": True,
            "status": "active",
            "agents": {
                "indexnow": {"status": "active"},  # canonical routes.indexnow key
                "backlink": {"status": "active"},
                "citation": {"status": "active"}
            },
            "metrics": {
                "urls_indexed": indexed,
                "backlinks_tracked": backlinks,
                "indexnow_pings_24h": today_pings,
                "ai_citations": get_ai_citation_stats()
            },
            "indexnow_configured": True  # canonical routes.indexnow key
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# POST /api/seo/indexnow/ping, /api/seo/indexnow/ping-all and /api/seo/run-cycle
# were removed 2026-09-11, with get_priority_urls, the URL list only they used.
# None of the three had an auth gate: an anonymous POST reached ping_indexnow
# and submitted URLs to IndexNow under our key. Verified live before removal: a
# malformed JSON body got the handler's own 400, where the gated
# /api/v1/admin/indexnow answers the same body with 401. Nothing called them
# (Railway: no requests in 7 days besides that probe; no reference in the
# backend, frontend or MCP repos). routes/indexnow.py is the submit path:
# admin-gated, and run every 6h by .github/workflows/indexnow.yml.


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


def register_seo_agent(app):
    """Register SEO agent with Flask app"""
    init_seo_tables()
    app.register_blueprint(seo_agent_bp)
    print("🔍 SEO Agent registered:")
    print("   GET  /api/seo/status - SEO metrics")
    print("   GET  /api/seo/content-ideas - Backlink content ideas")
    print("   GET  /api/seo/ai-citations - AI citation tracking")
    print("   GET  /api/seo/backlink-opportunities - Link opportunities")
