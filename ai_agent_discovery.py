"""
DC Hub — AI Agent Discovery Routes v2
======================================
Adds support for all major AI agent discovery protocols:
- AGENTS.md (OpenAI/Linux Foundation standard)
- Google Agent2Agent (A2A) Protocol
- llms-full.txt (extended LLM documentation)
- security.txt (RFC 9116)
- Enhanced AI platform tracking

Installation:
  1. Copy this file to your Replit project
  2. Copy the discovery files (AGENTS.md, llms-full.txt, .well-known/agent.json, .well-known/security.txt)
  3. Add to main.py:
       from ai_agent_discovery import register_discovery_routes
       register_discovery_routes(app)
  4. Restart Replit

New endpoints served:
  GET /AGENTS.md                      - AGENTS.md (Linux Foundation standard)
  GET /.well-known/agent.json         - Google A2A Agent Card
  GET /llms-full.txt                  - Extended LLM documentation
  GET /.well-known/security.txt       - Security contact (RFC 9116)
  POST /a2a/tasks/send                - A2A task handler
  GET /api/v1/ai-tracking/stats       - AI platform access statistics
  GET /api/v1/ai-tracking/recent      - Recent AI accesses
"""

import os
import json
import uuid
import logging
from datetime import datetime, timedelta
from functools import wraps
from flask import Blueprint, request, jsonify, Response, send_file, make_response
from db_utils import get_db, try_get_db

logger = logging.getLogger(__name__)

# Database path (same as main app)
DB_PATH = os.environ.get('DB_PATH', 'dc_nexus.db')

# =============================================================================
# AI ACCESS TRACKING
# =============================================================================

# Known AI platform user-agent patterns
AI_PLATFORMS = {
    'ChatGPT': ['ChatGPT', 'OpenAI', 'GPTBot'],
    'Claude': ['Claude', 'Anthropic', 'ClaudeBot'],
    'Perplexity': ['Perplexity', 'PerplexityBot'],
    'Gemini': ['Google-Extended', 'Googlebot', 'GoogleOther', 'Gemini'],
    'Bing/Copilot': ['bingbot', 'BingPreview', 'Copilot', 'msnbot'],
    'Codex': ['Codex', 'OpenAI-Codex'],
    'Cursor': ['Cursor'],
    'Cohere': ['Cohere', 'CohereBot', 'cohere-ai'],
    'Meta AI': ['Meta-ExternalAgent', 'FacebookBot', 'meta-externalagent'],
    'Apple': ['Applebot', 'AppleBot'],
    'Yandex': ['YandexBot'],
    'Moltbook': ['Moltbook', 'moltbook'],
    'OpenClaw': ['OpenClaw', 'openclaw'],
    'You.com': ['YouBot', 'youchat'],
    'DeepSeek': ['DeepSeek', 'Deepseek'],
}


def identify_ai_platform(user_agent):
    """Identify which AI platform is making the request"""
    if not user_agent:
        return 'Unknown'
    ua_lower = user_agent.lower()
    for platform, patterns in AI_PLATFORMS.items():
        for pattern in patterns:
            if pattern.lower() in ua_lower:
                return platform
    # Check for generic bot patterns
    if any(kw in ua_lower for kw in ['bot', 'crawler', 'spider', 'agent', 'ai']):
        return 'Bot/Crawler'
    return None  # Not an AI platform


def init_tracking_db():
    """Initialize the AI tracking database table"""
    conn = None
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS ai_access_log (
            id SERIAL PRIMARY KEY,
            timestamp TEXT NOT NULL,
            platform TEXT NOT NULL,
            user_agent TEXT,
            ip_address TEXT,
            file_requested TEXT NOT NULL,
            method TEXT DEFAULT 'GET',
            response_code INTEGER DEFAULT 200
        )''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_ai_access_timestamp 
                     ON ai_access_log(timestamp)''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_ai_access_platform 
                     ON ai_access_log(platform)''')
        conn.commit()
        logger.info("AI tracking database initialized")
    except Exception as e:
        logger.error(f"Failed to init tracking DB: {e}")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def log_ai_access(file_requested, platform=None):
    """Log an AI platform access to a discovery file. Non-blocking — skips if DB busy."""
    conn = None
    try:
        user_agent = request.headers.get('User-Agent', '')
        if platform is None:
            platform = identify_ai_platform(user_agent)
        if platform is None:
            return
        
        ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        if ip and ',' in ip:
            ip = ip.split(',')[0].strip()
        
        conn = try_get_db()
        if conn is None:
            return
        c = conn.cursor()
        c.execute('''INSERT INTO ai_access_log 
                     (timestamp, platform, user_agent, ip_address, file_requested)
                     VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING''',
                  (datetime.utcnow().isoformat(), platform, user_agent[:500], ip, file_requested))
        conn.commit()
    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# =============================================================================
# DISCOVERY FILE CONTENT
# =============================================================================

def load_file(filename):
    """Load a discovery file from disk"""
    # Try multiple paths
    paths = [
        filename,
        os.path.join(os.path.dirname(__file__), filename),
        os.path.join('/home/runner', filename),
    ]
    for path in paths:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                return f.read()
    return None


# Inline fallback content for AGENTS.md if file not found
AGENTS_MD_FALLBACK = """# DC Hub — Data Center Intelligence Platform

## Overview
DC Hub (dchub.cloud) tracks 50,000+ data center facilities across 140+ countries.
Free API at https://dchub.cloud/api/v1

## API Endpoints
- GET /api/v1/facilities%sq={query} - Search facilities
- GET /api/v1/transactions%slimit={n} - M&A deals
- GET /api/v1/news%slimit={n}&q={keyword} - Industry news
- GET /api/site-score%slat={lat}&lon={lon}&state={state} - Site scoring
- GET /api/grid/fuel-mix%siso={ISO} - Real-time grid data (ERCOT, PJM, CAISO, MISO, SPP, NYISO, ISONE)
- GET /api/carbon/intensity%sstate={state} - Carbon intensity
- GET /api/renewable/solar%slat={lat}&lon={lon} - Solar potential
- GET /api/renewable/wind%slat={lat}&lon={lon} - Wind potential
- GET /api/energy/prices/{state} - Electricity prices
- GET /api/v1/gas-pipelines%sstate={state} - Gas pipelines
- GET /api/market-report - Daily market report

## Auth: No key needed for 100 req/day. Pro: X-API-Key header.
## Docs: https://dchub.cloud/.well-known/openapi.json
"""

# Inline A2A Agent Card
A2A_AGENT_CARD = {
    "protocolVersion": "0.2.1",
    "name": "DC Hub Intelligence Agent",
    "description": "Data center intelligence platform - 50,000+ facilities, $51B+ M&A, real-time grid data from 7 ISOs, site scoring, market intelligence across 140+ countries.",
    "url": "https://dchub.cloud",
    "iconUrl": "https://dchub.cloud/favicon.ico",
    "version": "86.0.0",
    "provider": {
        "organization": "DC Hub",
        "url": "https://dchub.cloud"
    },
    "capabilities": {
        "streaming": False,
        "pushNotifications": False
    },
    "skills": [
        {
            "id": "facility-search",
            "name": "Data Center Facility Search",
            "description": "Search 50,000+ data center facilities worldwide by name, location, provider, or capacity.",
            "tags": ["data center", "colocation", "facility", "infrastructure"],
            "examples": ["Find Equinix data centers in Dallas", "List hyperscale data centers in Arizona"]
        },
        {
            "id": "site-scoring",
            "name": "Data Center Site Analysis",
            "description": "Score any location (0-100) for data center suitability: power, carbon, infrastructure, connectivity, risk.",
            "tags": ["site selection", "scoring", "power", "carbon", "renewable energy"],
            "examples": ["Score Ashburn VA for a data center", "Compare Phoenix vs Dallas for DC site"]
        },
        {
            "id": "grid-analytics",
            "name": "Real-Time Grid Analytics",
            "description": "Live power grid fuel mix from 7 US ISOs: ERCOT, PJM, CAISO, MISO, SPP, NYISO, ISONE.",
            "tags": ["power grid", "energy", "fuel mix", "ISO", "real-time"],
            "examples": ["What is ERCOT's current fuel mix?", "Show renewables on PJM grid"]
        },
        {
            "id": "ma-tracking",
            "name": "M&A Transaction Tracking",
            "description": "Track $51B+ in data center M&A, CapEx, and investment deals.",
            "tags": ["M&A", "transactions", "deals", "acquisitions", "investment"],
            "examples": ["Recent data center acquisitions", "Deals over $1 billion"]
        },
        {
            "id": "market-intelligence",
            "name": "Market Intelligence",
            "description": "Daily market reports: facility counts, capacity, deal volume, trends across 140+ countries.",
            "tags": ["market report", "intelligence", "analytics", "trends"],
            "examples": ["Today's market report", "Top data center markets globally"]
        },
        {
            "id": "news-aggregation",
            "name": "Industry News Feed",
            "description": "Real-time news from 60+ sources, updated every minute.",
            "tags": ["news", "industry", "data center"],
            "examples": ["Latest data center news", "News about hyperscale construction"]
        },
        {
            "id": "energy-infrastructure",
            "name": "Energy Infrastructure",
            "description": "Gas pipelines, electricity pricing, carbon intensity, solar/wind potential.",
            "tags": ["energy", "gas", "electricity", "carbon", "solar", "wind"],
            "examples": ["Gas pipelines in Texas", "Electricity prices in Virginia"]
        }
    ],
    "defaultInputModes": ["text/plain", "application/json"],
    "defaultOutputModes": ["application/json", "text/plain"],
    "authentication": {
        "schemes": [{
            "scheme": "apiKey",
            "in": "header",
            "name": "X-API-Key",
            "description": "Optional. Free tier: 100 req/day without key."
        }]
    }
}

SECURITY_TXT = """Contact: mailto:api@dchub.cloud
Preferred-Languages: en
Canonical: https://dchub.cloud/.well-known/security.txt
Expires: 2027-01-31T00:00:00.000Z
"""


# =============================================================================
# BLUEPRINT REGISTRATION
# =============================================================================

discovery_bp = Blueprint('discovery', __name__)


# ----- AGENTS.md -----
# AUTO-REPAIR: duplicate route '/AGENTS.md' also in ai_discovery_routes.py:290 — review and remove one
@discovery_bp.route('/AGENTS.md')
@discovery_bp.route('/agents.md')
def serve_agents_md():
    """Serve AGENTS.md - the open standard for AI coding agents"""
    log_ai_access('AGENTS.md')
    content = load_file('AGENTS.md') or AGENTS_MD_FALLBACK
    response = make_response(content)
    response.headers['Content-Type'] = 'text/markdown; charset=utf-8'
    response.headers['Cache-Control'] = 'public, max-age=3600'
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response


# AUTO-REPAIR: duplicate route '/.well-known/agent.json' also in main.py:16054 — review and remove one
# ----- Google A2A Agent Card -----
@discovery_bp.route('/.well-known/agent.json')
def serve_a2a_agent_card():
    """Serve A2A Agent Card for Google Agent2Agent Protocol discovery"""
    log_ai_access('agent.json')
    response = jsonify(A2A_AGENT_CARD)
    response.headers['Cache-Control'] = 'public, max-age=3600'
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response


# ----- A2A Task Handler -----
@discovery_bp.route('/a2a/tasks/send', methods=['POST'])
def a2a_task_send():
    """Handle Google A2A Protocol task requests (JSON-RPC 2.0)"""
    log_ai_access('a2a/tasks/send')
    
    try:
        data = request.json or {}
        rpc_id = data.get('id', str(uuid.uuid4()))
        params = data.get('params', {})
        message = params.get('message', {})
        parts = message.get('parts', [])
        
        # Extract query text from parts
        query = ''
        for part in parts:
            if isinstance(part, dict) and part.get('type') == 'text':
                query = part.get('text', '')
                break
        
        if not query:
            return jsonify({
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "id": str(uuid.uuid4()),
                    "status": {"state": "completed"},
                    "artifacts": [{
                        "parts": [{"type": "text", "text": json.dumps({
                            "message": "DC Hub Intelligence Agent ready. Ask about data center facilities, M&A deals, grid data, site scoring, or industry news.",
                            "endpoints": {
                                "facilities": "/api/v1/facilities%sq={query}",
                                "transactions": "/api/v1/transactions",
                                "news": "/api/v1/news",
                                "site_score": "/api/site-score%slat={lat}&lon={lon}&state={state}",
                                "grid": "/api/grid/fuel-mix%siso={iso}"
                            }
                        })}]
                    }]
                }
            })
        
        # Route query to appropriate endpoint
        result = route_a2a_query(query)
        
        return jsonify({
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "id": str(uuid.uuid4()),
                "status": {"state": "completed"},
                "artifacts": [{
                    "parts": [{"type": "text", "text": json.dumps(result)}]
                }]
            }
        })
    
    except Exception as e:
        logger.error(f"A2A task error: {e}")
        return jsonify({
            "jsonrpc": "2.0",
            "id": data.get('id', ''),
            "error": {
                "code": -32603,
                "message": f"Internal error: {str(e)}"
            }
        }), 500


def route_a2a_query(query):
    """Route an A2A query to the appropriate DC Hub endpoint"""
    import requests as req
    
    q_lower = query.lower()
    base = "http://localhost:5000"  # Internal routing
    
    try:
        # Site scoring
        if any(kw in q_lower for kw in ['score', 'evaluate', 'site', 'analyze location']):
            return {
                "type": "guidance",
                "message": "Use the site scoring endpoint for location analysis.",
                "endpoint": "/api/site-score?lat={lat}&lon={lon}&state={state}",
                "example": "GET /api/site-score?lat=39.0438&lon=-77.4874&state=VA"
            }
        
        # Grid/energy
        if any(kw in q_lower for kw in ['grid', 'fuel mix', 'ercot', 'pjm', 'caiso', 'miso', 'energy', 'power']):
            # Try to extract ISO
            iso = None
            for i in ['ERCOT', 'PJM', 'CAISO', 'MISO', 'SPP', 'NYISO', 'ISONE']:
                if i.lower() in q_lower:
                    iso = i
                    break
            if iso:
                try:
                    r = req.get(f"{base}/api/grid/fuel-mix?iso={iso}", timeout=30)
                    if r.ok:
                        return {"type": "grid_data", "iso": iso, "data": r.json()}
                except:
                    pass
            return {
                "type": "guidance",
                "message": "Use grid fuel mix endpoint.",
                "endpoint": "/api/grid/fuel-mix?iso={ISO}",
                "supported_isos": ["ERCOT", "PJM", "CAISO", "MISO", "SPP", "NYISO", "ISONE"]
            }
        
        # M&A / deals
        if any(kw in q_lower for kw in ['deal', 'transaction', 'acquisition', 'm&a', 'merger', 'investment']):
            try:
                r = req.get(f"{base}/api/v1/transactions%slimit=10", timeout=30)
                if r.ok:
                    return {"type": "transactions", "data": r.json()}
            except:
                pass
            return {"type": "guidance", "endpoint": "/api/v1/transactions%slimit=10"}
        
        # News
        if any(kw in q_lower for kw in ['news', 'latest', 'headline', 'announcement', 'update']):
            try:
                r = req.get(f"{base}/api/v1/news%slimit=10", timeout=30)
                if r.ok:
                    return {"type": "news", "data": r.json()}
            except:
                pass
            return {"type": "guidance", "endpoint": "/api/v1/news%slimit=10"}
        
        # Default: facility search
        try:
            r = req.get(f"{base}/api/v1/facilities%sq={query}&limit=10", timeout=30)
            if r.ok:
                return {"type": "facilities", "query": query, "data": r.json()}
        except:
            pass
        
        return {
            "type": "guidance",
            "message": f"DC Hub can help with: '{query}'. Use our API endpoints.",
            "endpoints": {
                "facilities": f"/api/v1/facilities?q={query}",
                "transactions": "/api/v1/transactions",
                "news": "/api/v1/news",
                "site_score": "/api/site-score?lat=LAT&lon=LON&state=ST",
                "grid": "/api/grid/fuel-mix?iso=ERCOT"
            }
        }
    
    except Exception as e:
        return {"type": "error", "message": str(e)}

# AUTO-REPAIR: duplicate route '/llms-full.txt' also in ai_discovery_routes.py:405 — review and remove one

# ----- llms-full.txt -----
@discovery_bp.route('/llms-full.txt')
def serve_llms_full():
    """Serve extended LLM documentation"""
    log_ai_access('llms-full.txt')
    content = load_file('llms-full.txt')
    if not content:
        content = "# DC Hub Full API Documentation\n# See https://dchub.cloud/llms.txt for summary\n# API Base: https://dchub.cloud/api/v1\n"
    response = make_response(content)
    response.headers['Content-Type'] = 'text/plain; charset=utf-8'
    response.headers['Cache-Control'] = 'public, max-age=3600'
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response
# AUTO-REPAIR: duplicate route '/.well-known/security.txt' also in main.py:16074 — review and remove one


# ----- security.txt -----
@discovery_bp.route('/.well-known/security.txt')
def serve_security_txt():
    """Serve security.txt per RFC 9116"""
    log_ai_access('security.txt')
    response = make_response(SECURITY_TXT)
    response.headers['Content-Type'] = 'text/plain; charset=utf-8'
    response.headers['Cache-Control'] = 'public, max-age=86400'
    response.headers['Access-Control-Allow-Origin'] = '*'
# AUTO-REPAIR: duplicate route '/api/v1/ai-tracking/stats' also in main.py:11840 — review and remove one
    return response


# ----- AI Tracking Stats -----
@discovery_bp.route('/api/v1/ai-tracking/stats')
def ai_tracking_stats():
    """Get AI platform access statistics"""
    try:
        conn = get_db()
        try:
            c = conn.cursor()

            # Total accesses
            c.execute("SELECT COUNT(*) FROM ai_access_log")
            total = c.fetchone()[0]

            # By platform
            c.execute("""SELECT platform, COUNT(*) as cnt
                        FROM ai_access_log
                        GROUP BY platform
                        ORDER BY cnt DESC""")
            by_platform = [{"platform": row[0], "count": row[1]} for row in c.fetchall()]

            # By file
            c.execute("""SELECT file_requested, COUNT(*) as cnt
                        FROM ai_access_log
                        GROUP BY file_requested
                        ORDER BY cnt DESC""")
            by_file = [{"file": row[0], "count": row[1]} for row in c.fetchall()]

            # Last 7 days trend
            week_ago = (datetime.utcnow() - timedelta(days=7)).isoformat()
            c.execute("""SELECT DATE(timestamp) as day, COUNT(*) as cnt
                        FROM ai_access_log
                        WHERE timestamp > %s
                        GROUP BY DATE(timestamp)
                        ORDER BY day""", (week_ago,))
            daily_trend = [{"date": row[0], "count": row[1]} for row in c.fetchall()]

            # Last 7 days by platform
            c.execute("""SELECT platform, COUNT(*) as cnt
                        FROM ai_access_log
                        WHERE timestamp > %s
                        GROUP BY platform
                        ORDER BY cnt DESC""", (week_ago,))
            weekly_by_platform = [{"platform": row[0], "count": row[1]} for row in c.fetchall()]

            # Today's count
            today = datetime.utcnow().strftime('%Y-%m-%d')
            c.execute("SELECT COUNT(*) FROM ai_access_log WHERE DATE(timestamp) = %s", (today,))
            today_count = c.fetchone()[0]

        finally:
            conn.close()
        
        return jsonify({
            "success": True,
            "data": {
                "total_accesses": total,
                "today": today_count,
                "by_platform": by_platform,
                "by_file": by_file,
                "daily_trend_7d": daily_trend,
                "weekly_by_platform": weekly_by_platform
            },
            "discovery_files": {
                "AGENTS.md": "/AGENTS.md",
                "agent.json (A2A)": "/.well-known/agent.json",
                "llms.txt": "/llms.txt",
                "llms-full.txt": "/llms-full.txt",
                "skill.md": "/skill.md",
                "openapi.json": "/.well-known/openapi.json",
                "mcp.json": "/.well-known/mcp.json",
                "ai-plugin.json": "/.well-known/ai-plugin.json",
                "copilot-agent.json": "/.well-known/copilot-agent.json",
                "security.txt": "/.well-known/security.txt"
            }
        })
    
    except Exception as e:
        logger.error(f"Tracking stats error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@discovery_bp.route('/api/v1/ai-tracking/recent')
def ai_tracking_recent():
    """Get recent AI platform accesses"""
    try:
        limit = min(int(request.args.get('limit', 50)), 200)
        conn = get_db()
        try:
            c = conn.cursor()
            c.execute("""SELECT timestamp, platform, file_requested, ip_address, user_agent
                        FROM ai_access_log
                        ORDER BY timestamp DESC
                        LIMIT %s""", (limit,))
            accesses = [{
                "timestamp": row[0],
                "platform": row[1],
                "file": row[2],
                "ip": row[3],
                "user_agent": row[4][:100] if row[4] else None
            } for row in c.fetchall()]
        finally:
            conn.close()
        
        return jsonify({"success": True, "data": accesses, "meta": {"count": len(accesses)}})
    
# AUTO-REPAIR: duplicate route '/api/v1/discovery' also in main.py:11957 — review and remove one
# AUTO-REPAIR: duplicate route '/ai/discovery' also in main.py:11958 — review and remove one
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ----- Discovery index -----
@discovery_bp.route('/api/v1/discovery')
@discovery_bp.route('/ai/discovery')
def discovery_index():
    """List all AI discovery files and their status"""
    log_ai_access('discovery-index')
    
    files = {
        "protocols": {
            "agents_md": {
                "url": "https://dchub.cloud/AGENTS.md",
                "standard": "AGENTS.md (Linux Foundation / OpenAI)",
                "description": "Open standard for AI coding agents. Read by Codex, Cursor, Jules, Aider.",
                "status": "active"
            },
            "a2a_agent_card": {
                "url": "https://dchub.cloud/.well-known/agent.json",
                "standard": "Google Agent2Agent (A2A) Protocol",
                "description": "Agent discovery card for Google's A2A protocol. Used by Vertex AI, ADK agents.",
                "status": "active"
            },
            "mcp": {
                "url": "https://dchub.cloud/.well-known/mcp.json",
                "standard": "Anthropic Model Context Protocol (MCP)",
                "description": "Claude MCP integration manifest.",
                "status": "active"
            },
            "openapi": {
                "url": "https://dchub.cloud/.well-known/openapi.json",
                "standard": "OpenAPI 3.1",
                "description": "Full API specification for any platform.",
                "status": "active"
            },
            "chatgpt_plugin": {
                "url": "https://dchub.cloud/.well-known/ai-plugin.json",
                "standard": "ChatGPT Plugin / GPT Actions",
                "description": "ChatGPT plugin manifest. Powers 3 custom GPTs.",
                "status": "active"
            },
            "copilot": {
                "url": "https://dchub.cloud/.well-known/copilot-agent.json",
                "standard": "Microsoft Copilot",
                "description": "Microsoft Copilot agent manifest.",
                "status": "active"
            }
        },
        "discovery_files": {
            "llms_txt": {
                "url": "https://dchub.cloud/llms.txt",
                "description": "LLM discovery file (summary)"
            },
            "llms_full_txt": {
                "url": "https://dchub.cloud/llms-full.txt",
                "description": "Extended LLM documentation with full API reference"
            },
            "skill_md": {
                "url": "https://dchub.cloud/skill.md",
                "description": "Moltbook-compatible skill file"
            },
            "skill_json": {
                "url": "https://dchub.cloud/skill.json",
                "description": "Machine-readable skill metadata"
            },
            "ai_txt": {
                "url": "https://dchub.cloud/ai.txt",
                "description": "AI metadata file"
            },
            "ai_agents_json": {
                "url": "https://dchub.cloud/.well-known/ai-agents.json",
                "description": "Generic AI agent discovery manifest"
            },
            "security_txt": {
                "url": "https://dchub.cloud/.well-known/security.txt",
                "description": "Security contact (RFC 9116)"
            },
            "robots_txt": {
                "url": "https://dchub.cloud/robots.txt",
                "description": "Crawler access rules"
            }
        },
        "chatgpt_gpts": [
            {
                "name": "DC Hub - Data Center Intelligence",
                "url": "https://chatgpt.com/g/g-697dda8f65e8819189f9d353725cb6d5-dc-hub-data-center-intelligence",
                "actions": ["getSiteScore", "getGridFuelMix", "getCarbonIntensity", "getSolarPotential", "getWindPotential", "getGasPipelines", "getEnergyPrices"]
            },
            {
                "name": "Data Center M&A Analyst",
                "url": "https://chatgpt.com/g/g-697e373bb1c88191b97fc323b2a32166-data-center-m-a-analyst",
                "actions": ["getTransactions", "getNews", "getMarkets"]
            },
            {
                "name": "Data Center News Briefing",
                "url": "https://chatgpt.com/g/g-697e43e749a081919cefcef68fbfe983-data-center-news-briefing",
                "actions": ["getNews", "getTransactions"]
            }
        ],
        "a2a_task_endpoint": "https://dchub.cloud/a2a/tasks/send",
        "tracking": "https://dchub.cloud/api/v1/ai-tracking/stats"
    }
    
    return jsonify({"success": True, "data": files})


# =============================================================================
# REGISTRATION FUNCTION
# =============================================================================

def register_discovery_routes(app):
    """Register all AI agent discovery routes with the Flask app"""
    
    # Initialize tracking database
    init_tracking_db()
    
    # Register blueprint
    app.register_blueprint(discovery_bp)
    
    logger.info("🤖 AI Agent Discovery v2: ✅ Registered")
    logger.info("   ├── AGENTS.md (Linux Foundation)")
    logger.info("   ├── A2A Agent Card (Google)")
    logger.info("   ├── llms-full.txt (Extended docs)")
    logger.info("   ├── security.txt (RFC 9116)")
    logger.info("   ├── A2A Task Handler (/a2a/tasks/send)")
    logger.info("   └── AI Tracking (/api/v1/ai-tracking/stats)")
    
    return discovery_bp
