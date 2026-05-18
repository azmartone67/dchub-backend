"""
DC Hub Nexus - MCP (Model Context Protocol) Server
===================================================
Enables AI platforms (Claude, ChatGPT, Gemini, etc.) to query DC Hub data.

This implements the MCP specification to expose DC Hub as a tool for AI assistants.
When AI systems need data center information, they can query this server.

Endpoints:
  GET  /mcp/manifest     - MCP manifest describing available tools
  POST /mcp/tools/call   - Execute a tool call
  GET  /mcp/health       - Health check

Tools Exposed:
  - search_facilities: Search data center facilities by location, provider, capacity
  - get_market_stats: Get market intelligence for a specific location
  - get_news: Get latest data center industry news
  - get_deals: Get M&A deals and transactions
  - analyze_site: Analyze a location for data center suitability
"""

import json
import sqlite3
from datetime import datetime
from typing import Dict, List, Any, Optional
from flask import Blueprint, request, jsonify
from db_utils import get_db

mcp_bp = Blueprint('mcp', __name__)

MCP_VERSION = "2025-11-25"
MCP_SERVER_NAME = "dc-hub-nexus"
MCP_SERVER_VERSION = "1.0.0"

def get_db_connection():
    """Get database connection"""
    try:
        conn = get_db()
        # sqlite3.Row removed - PostgreSQL uses RealDictCursor or dict(row)
        return conn
    except:
        return None


TOOL_DEFINITIONS = [
    {
        "name": "search_facilities",
        "description": "Search data center facilities worldwide. Filter by location (city, state, country), provider/operator name, capacity (MW), or status. Returns facility details including name, location, power capacity, and contact info.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query - can be city name, country, provider name, or keyword"
                },
                "country": {
                    "type": "string",
                    "description": "Filter by country code (e.g., 'US', 'DE', 'JP')"
                },
                "min_capacity_mw": {
                    "type": "number",
                    "description": "Minimum power capacity in megawatts"
                },
                "provider": {
                    "type": "string",
                    "description": "Filter by provider/operator name"
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results to return (default 10, max 50)",
                    "default": 10
                }
            }
        }
    },
    {
        "name": "get_market_stats",
        "description": "Get data center market statistics for a specific location or market. Returns facility count, total capacity, major providers, growth trends, and power availability.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "market": {
                    "type": "string",
                    "description": "Market name (e.g., 'Northern Virginia', 'Dallas', 'Frankfurt', 'Singapore')"
                },
                "country": {
                    "type": "string",
                    "description": "Country code for national stats"
                }
            },
            "required": []
        }
    },
    {
        "name": "get_news",
        "description": "Get latest data center industry news. Includes M&A activity, new facility announcements, capacity expansions, and market developments.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "Filter by topic (e.g., 'acquisition', 'expansion', 'hyperscale', 'sustainability')"
                },
                "company": {
                    "type": "string",
                    "description": "Filter by company name"
                },
                "days": {
                    "type": "integer",
                    "description": "Number of days of news to retrieve (default 7)",
                    "default": 7
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum articles to return (default 10)",
                    "default": 10
                }
            }
        }
    },
    {
        "name": "get_deals",
        "description": "Get data center M&A deals and transactions. Includes acquisitions, sales, and major investments with deal values and parties involved.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "deal_type": {
                    "type": "string",
                    "description": "Filter by type: 'acquisition', 'sale', 'investment', 'merger'"
                },
                "min_value": {
                    "type": "number",
                    "description": "Minimum deal value in millions USD"
                },
                "company": {
                    "type": "string",
                    "description": "Filter by company involved"
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum deals to return",
                    "default": 10
                }
            }
        }
    },
    {
        "name": "analyze_site",
        "description": "Analyze a location for data center suitability. Returns power availability, fiber connectivity, proximity to existing facilities, natural disaster risk, and market demand scores.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "Location to analyze (city, address, or coordinates)"
                },
                "latitude": {
                    "type": "number",
                    "description": "Latitude coordinate"
                },
                "longitude": {
                    "type": "number",
                    "description": "Longitude coordinate"
                }
            },
            "required": []
        }
    },
    {
        "name": "get_providers",
        "description": "Get information about data center providers/operators. Returns company details, facility count, total capacity, and geographic presence.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Provider name to look up"
                },
                "top": {
                    "type": "integer",
                    "description": "Get top N providers by capacity",
                    "default": 10
                }
            }
        }
    },
    {
        "name": "get_capacity_pipeline",
        "description": "Get announced data center capacity expansions and new builds. Returns projects with MW estimates, operators, locations, and timelines.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "operator": {
                    "type": "string",
                    "description": "Filter by operator/developer name"
                },
                "market": {
                    "type": "string",
                    "description": "Filter by market/region"
                },
                "min_mw": {
                    "type": "number",
                    "description": "Minimum capacity in megawatts"
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results to return",
                    "default": 20
                }
            }
        }
    }
]


# AUTO-REPAIR: duplicate route '/mcp/manifest' also in main.py:5340 — review and remove one
@mcp_bp.route('/mcp/manifest', methods=['GET'])
def get_manifest():
    """Return MCP manifest describing this server's capabilities"""
    manifest = {
        "name": MCP_SERVER_NAME,
        "version": MCP_SERVER_VERSION,
        "protocolVersion": MCP_VERSION,
        "description": "DC Hub Nexus - Data center intelligence platform with 10,400+ facilities, M&A deal tracking, capacity pipeline, and infrastructure mapping worldwide.",
        "vendor": "DC Hub",
        "capabilities": {
            "tools": True,
            "resources": False,
            "prompts": False
        },
        "tools": TOOL_DEFINITIONS,
        "contact": {
            "url": "https://dchub.cloud",
            "documentation": "https://dchub.cloud/api/docs"
        }
    }
    return jsonify(manifest)


@mcp_bp.route('/mcp/tools/call', methods=['POST'])
def call_tool():
    """Execute a tool call from an AI system"""
    try:
        data = request.get_json()
        tool_name = data.get('name')
        arguments = data.get('arguments', {})
        
        if tool_name == 'search_facilities':
            result = _search_facilities(arguments)
        elif tool_name == 'get_market_stats':
            result = _get_market_stats(arguments)
        elif tool_name == 'get_news':
            result = _get_news(arguments)
        elif tool_name == 'get_deals':
            result = _get_deals(arguments)
        elif tool_name == 'analyze_site':
            result = _analyze_site(arguments)
        elif tool_name == 'get_providers':
            result = _get_providers(arguments)
        elif tool_name == 'get_capacity_pipeline':
            result = _get_capacity_pipeline(arguments)
        else:
            return jsonify({
                "error": {
                    "code": "unknown_tool",
                    "message": f"Unknown tool: {tool_name}"
                }
            }), 400
        
        return jsonify({
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result, indent=2, default=str)
                }
            ]
        })
        
    except Exception as e:
        return jsonify({
            "error": {
                "code": "execution_error",
                "message": str(e)
            }
        }), 500


@mcp_bp.route('/mcp/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    conn = get_db_connection()
    db_status = "connected" if conn else "disconnected"
    if conn:
        conn.close()
    
    return jsonify({
        "status": "healthy",
        "server": MCP_SERVER_NAME,
        "version": MCP_SERVER_VERSION,
        "protocol": MCP_VERSION,
        "database": db_status,
        "timestamp": datetime.utcnow().isoformat()
    })

# AUTO-REPAIR: duplicate route '/.well-known/mcp.json' also in main.py:15873 — review and remove one

@mcp_bp.route('/.well-known/mcp.json', methods=['GET'])
def well_known_mcp():
    """Well-known endpoint for MCP discovery - serves static file"""
    from flask import send_from_directory
    import os
    try:
        return send_from_directory('.well-known', 'mcp.json', mimetype='application/json')
    except:
        return jsonify({
            "mcp_server": "/mcp/manifest",
            "name": MCP_SERVER_NAME,
            "version": "2.0.0",
            "description": "Data center intelligence platform with 50,000+ facilities, infrastructure mapping, and water/drought analysis"
        })


def _search_facilities(args: Dict) -> Dict:
    """Search facilities in the database"""
    conn = get_db_connection()
    if not conn:
        return {"error": "Database unavailable", "facilities": []}
    
    try:
        query = args.get('query', '')
        country = args.get('country', '')
        provider = args.get('provider', '')
        min_capacity = args.get('min_capacity_mw', 0)
        limit = min(args.get('limit', 10), 50)
        
        sql = "SELECT * FROM facilities WHERE 1=1"
        params = []
        
        if query:
            sql += " AND (name LIKE %s OR city LIKE %s OR region LIKE %s OR provider LIKE %s)"
            q = f"%{query}%"
            params.extend([q, q, q, q])
        
        if country:
            sql += " AND country = %s"
            params.append(country.upper())
        
        if provider:
            sql += " AND provider LIKE %s"
            params.append(f"%{provider}%")
        
        if min_capacity > 0:
            sql += " AND CAST(power_mw AS REAL) >= %s"
            params.append(min_capacity)
        
        sql += f" ORDER BY CAST(power_mw AS REAL) DESC LIMIT {limit}"
        
        c = conn.cursor()
        cursor = c.execute(sql, params)
        rows = cursor.fetchall()
        
        facilities = []
        for row in rows:
            facilities.append({
                "name": row['name'],
                "provider": row['provider'],
                "city": row['city'],
                "region": row['region'],
                "country": row['country'],
                "power_mw": row['power_mw'],
                "status": row['status'],
                "latitude": row['latitude'],
                "longitude": row['longitude'],
                "source": row['source']
            })
        
        conn.close()
        return {
            "total_results": len(facilities),
            "facilities": facilities
        }
        
    except Exception as e:
        conn.close()
        return {"error": str(e), "facilities": []}


def _get_market_stats(args: Dict) -> Dict:
    """Get market statistics"""
    conn = get_db_connection()
    if not conn:
        return {"error": "Database unavailable"}
    
    try:
        market = args.get('market', '')
        country = args.get('country', '')
        
        if market:
            sql = """
                SELECT 
                    COUNT(*) as facility_count,
                    SUM(CAST(power_mw AS REAL)) as total_mw,
                    COUNT(DISTINCT provider) as provider_count
                FROM facilities 
                WHERE city LIKE %s OR region LIKE %s
            """
            c = conn.cursor()
            cursor = c.execute(sql, [f"%{market}%", f"%{market}%"])
        elif country:
            sql = """
                SELECT 
                    COUNT(*) as facility_count,
                    SUM(CAST(power_mw AS REAL)) as total_mw,
                    COUNT(DISTINCT provider) as provider_count
                FROM facilities 
                WHERE country = %s
            """
            c = conn.cursor()
            cursor = c.execute(sql, [country.upper()])
        else:
            sql = """
                SELECT 
                    COUNT(*) as facility_count,
                    SUM(CAST(power_mw AS REAL)) as total_mw,
                    COUNT(DISTINCT provider) as provider_count,
                    COUNT(DISTINCT country) as country_count
                FROM facilities
            """
            c = conn.cursor()
            cursor = c.execute(sql)
        
        row = cursor.fetchone()
        
        top_providers_sql = """
            SELECT provider, COUNT(*) as count, SUM(CAST(power_mw AS REAL)) as total_mw
            FROM facilities 
            WHERE provider IS NOT NULL AND provider != ''
            GROUP BY provider 
            ORDER BY total_mw DESC 
            LIMIT 10
        """
        c = conn.cursor()
        top_providers = c.execute(top_providers_sql).fetchall()
        
        conn.close()
        
        return {
            "market": market or country or "Global",
            "facility_count": row['facility_count'] or 0,
            "total_capacity_mw": round(row['total_mw'] or 0, 1),
            "provider_count": row['provider_count'] or 0,
            "top_providers": [
                {"name": p['provider'], "facilities": p['count'], "capacity_mw": round(p['total_mw'] or 0, 1)}
                for p in top_providers
            ],
            "data_source": "DC Hub Nexus",
            "last_updated": datetime.utcnow().isoformat()
        }
        
    except Exception as e:
        conn.close()
        return {"error": str(e)}


def _get_news(args: Dict) -> Dict:
    """Get recent news articles"""
    conn = get_db_connection()
    if not conn:
        return {"error": "Database unavailable", "articles": []}
    
    try:
        topic = args.get('topic', '')
        company = args.get('company', '')
        days = args.get('days', 7)
        limit = min(args.get('limit', 10), 50)
        
        sql = "SELECT * FROM announcements WHERE 1=1"
        params = []
        
        if topic:
            sql += " AND (title LIKE %s OR summary LIKE %s)"
            t = f"%{topic}%"
            params.extend([t, t])
        
        if company:
            sql += " AND (title LIKE %s OR companies LIKE %s)"
            c = f"%{company}%"
            params.extend([c, c])
        
        sql += " ORDER BY published_date DESC LIMIT %s"
        params.append(limit)
        
        c = conn.cursor()
        cursor = c.execute(sql, params)
        rows = cursor.fetchall()
        
        articles = []
        for row in rows:
            articles.append({
                "title": row['title'],
                "summary": row['summary'],
                "source": row['source'],
                "url": row['url'],
                "published_date": row['published_date'],
                "companies": row['companies'],
                "locations": row['locations']
            })
        
        conn.close()
        return {
            "total_results": len(articles),
            "articles": articles
        }
        
    except Exception as e:
        conn.close()
        return {"error": str(e), "articles": []}


def _get_deals(args: Dict) -> Dict:
    """Get M&A deals and transactions"""
    try:
        from transactions_news_api import VERIFIED_TRANSACTIONS
        
        deal_type = args.get('deal_type', '').lower()
        min_value = args.get('min_value', 0)
        company = args.get('company', '').lower()
        limit = min(args.get('limit', 10), 50)
        
        deals = []
        for deal in VERIFIED_TRANSACTIONS:
            if deal_type and deal_type not in deal.get('type', '').lower():
                continue
            
            value = deal.get('deal_value_usd', 0) or 0
            if min_value > 0 and value < min_value * 1_000_000:
                continue
            
            if company:
                parties = f"{deal.get('buyer', '')} {deal.get('seller', '')} {deal.get('target', '')}".lower()
                if company not in parties:
                    continue
            
            deals.append({
                "title": deal.get('title'),
                "buyer": deal.get('buyer'),
                "seller": deal.get('seller'),
                "target": deal.get('target'),
                "deal_value_usd": deal.get('deal_value_usd'),
                "deal_type": deal.get('type'),
                "date": deal.get('date'),
                "description": deal.get('description')
            })
            
            if len(deals) >= limit:
                break
        
        return {
            "total_results": len(deals),
            "deals": deals
        }
        
    except Exception as e:
        return {"error": str(e), "deals": []}


def _analyze_site(args: Dict) -> Dict:
    """Analyze a location for data center suitability"""
    location = args.get('location', '')
    lat = args.get('latitude')
    lon = args.get('longitude')
    
    conn = get_db_connection()
    if not conn:
        return {"error": "Database unavailable"}
    
    try:
        if lat and lon:
            sql = """
                SELECT COUNT(*) as nearby_count,
                       AVG(CAST(power_mw AS REAL)) as avg_capacity
                FROM facilities 
                WHERE latitude BETWEEN %s AND %s
                AND longitude BETWEEN %s AND %s
            """
            c = conn.cursor()
            cursor = c.execute(sql, [lat - 0.5, lat + 0.5, lon - 0.5, lon + 0.5])
        else:
            sql = """
                SELECT COUNT(*) as nearby_count,
                       AVG(CAST(power_mw AS REAL)) as avg_capacity
                FROM facilities 
                WHERE city LIKE %s OR region LIKE %s
            """
            c = conn.cursor()
            cursor = c.execute(sql, [f"%{location}%", f"%{location}%"])
        
        row = cursor.fetchone()
        nearby = row['nearby_count'] or 0
        avg_cap = row['avg_capacity'] or 0
        
        market_score = min(100, nearby * 5 + 20)
        competition = "high" if nearby > 20 else "medium" if nearby > 5 else "low"
        
        conn.close()
        
        return {
            "location": location or f"{lat}, {lon}",
            "analysis": {
                "nearby_facilities": nearby,
                "average_capacity_mw": round(avg_cap, 1),
                "market_maturity_score": market_score,
                "competition_level": competition,
                "recommendation": "Established market" if nearby > 10 else "Emerging opportunity" if nearby > 3 else "Greenfield opportunity"
            },
            "note": "Full site analysis including power grid, fiber, and risk factors available via premium API"
        }
        
    except Exception as e:
        conn.close()
        return {"error": str(e)}


def _get_providers(args: Dict) -> Dict:
    """Get provider/operator information"""
    conn = get_db_connection()
    if not conn:
        return {"error": "Database unavailable"}
    
    try:
        name = args.get('name', '')
        top = args.get('top', 10)
        
        if name:
            sql = """
                SELECT provider, 
                       COUNT(*) as facility_count,
                       SUM(CAST(power_mw AS REAL)) as total_mw,
                       GROUP_CONCAT(DISTINCT country) as countries
                FROM facilities 
                WHERE provider LIKE %s
                GROUP BY provider
            """
            c = conn.cursor()
            cursor = c.execute(sql, [f"%{name}%"])
        else:
            sql = f"""
                SELECT provider, 
                       COUNT(*) as facility_count,
                       SUM(CAST(power_mw AS REAL)) as total_mw,
                       COUNT(DISTINCT country) as country_count
                FROM facilities 
                WHERE provider IS NOT NULL AND provider != ''
                GROUP BY provider
                ORDER BY total_mw DESC
                LIMIT {top}
            """
            c = conn.cursor()
            cursor = c.execute(sql)
        
        rows = cursor.fetchall()
        
        providers = []
        for row in rows:
            providers.append({
                "name": row['provider'],
                "facility_count": row['facility_count'],
                "total_capacity_mw": round(row['total_mw'] or 0, 1),
                "country_count": row.get('country_count') or len((row.get('countries') or '').split(','))
            })
        
        conn.close()
        return {
            "total_results": len(providers),
            "providers": providers
        }
        
    except Exception as e:
        conn.close()
        return {"error": str(e)}


def _get_capacity_pipeline(args: Dict) -> Dict:
    """Get capacity pipeline data"""
    conn = get_db_connection()
    if not conn:
        return {"error": "Database unavailable"}
    
    try:
        operator = args.get('operator', '')
        market = args.get('market', '')
        min_mw = args.get('min_mw', 0)
        limit = min(args.get('limit', 20), 100)
        
        sql = """
            SELECT operator, market, capacity_mw, status, source, 
                   announced_date, estimated_completion
            FROM capacity_pipeline
            WHERE 1=1
        """
        params = []
        
        if operator:
            sql += " AND operator LIKE %s"
            params.append(f"%{operator}%")
        if market:
            sql += " AND market LIKE %s"
            params.append(f"%{market}%")
        if min_mw:
            sql += " AND capacity_mw >= %s"
            params.append(min_mw)
        
        sql += f" ORDER BY capacity_mw DESC LIMIT {limit}"
        
        c = conn.cursor()
        cursor = c.execute(sql, params)
        rows = cursor.fetchall()
        
        pipeline = []
        total_mw = 0
        for row in rows:
            mw = row['capacity_mw'] or 0
            total_mw += mw
            pipeline.append({
                "operator": row['operator'],
                "market": row['market'],
                "capacity_mw": mw,
                "status": row['status'],
                "source": row['source'],
                "announced_date": row['announced_date'],
                "estimated_completion": row['estimated_completion']
            })
        
        conn.close()
        return {
            "total_results": len(pipeline),
            "total_capacity_mw": round(total_mw, 1),
            "pipeline": pipeline,
            "citation": "Source: DC Hub Nexus (dchub.cloud)"
        }
        
    except Exception as e:
        conn.close()
        return {"error": str(e), "pipeline": []}


def register_mcp_routes(app):
    """Register MCP routes with Flask app"""
    app.register_blueprint(mcp_bp)
    print("🤖 MCP Server registered - AI platforms can now query DC Hub")
