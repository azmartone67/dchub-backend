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
import logging
from datetime import datetime
from functools import wraps
from flask import Blueprint, request, jsonify, Response, send_file
from db_utils import get_db, try_get_db
from workos_authkit import authkit_endpoints, AUTHKIT_SCOPES
from ai_surface_canon import canon_text

logger = logging.getLogger(__name__)

# Database path (same as main app)
DB_PATH = os.environ.get('DB_PATH', 'dc_nexus.db')

# =============================================================================
# AI ACCESS TRACKING
# =============================================================================

# Known AI platform user-agent patterns.
# Match is case-insensitive substring (see identify_ai_platform).
# Add a vendor here when you want UA-sniff auto-issue (main.py:1199) to mint
# a trial key on their first hit + the dashboard to classify them as Active.
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
    # r-ua-expand (2026-06-04): unlocking auto-issue for the 9 cold-pitch
    # labs that didn't have UA patterns yet. SDK paths from each vendor's
    # public Python/JS clients + any documented bot UAs they ship.
    'Mistral':     ['Mistral', 'mistralai', 'mistral-ai', 'MistralBot'],
    'Groq':        ['Groq', 'GroqBot', 'groq-sdk', 'groq-python', 'groq-ai'],
    'HuggingFace': ['HuggingFace', 'huggingface', 'hf-inference',
                     'transformers/', 'hf-hub', 'huggingface_hub'],
    'Grok/xAI':    ['Grok', 'xAI', 'grok-ai', 'GrokBot'],
    'CoreWeave':   ['CoreWeave', 'coreweave', 'coreweave-ai'],
    'Lambda':      ['LambdaBot', 'lambdalabs', 'lambda-ai', 'Lambda-Inference'],
    'TensorWave':  ['TensorWave', 'tensorwave'],
    'NVIDIA':      ['NVIDIA-Inference', 'nvidia-bot', 'NIM-Bot'],
    'Core42':      ['Core42', 'core42', 'G42-AI', 'g42-ai'],
    # Adjacent labs likely to integrate via MCP/REST in the next quarter.
    'AI21':        ['ai21', 'AI21', 'Jurassic'],
    'Inflection':  ['inflection', 'pi-bot', 'Inflection-AI'],
    'Adept':       ['Adept', 'adeptai'],
    'Replicate':   ['replicate', 'Replicate-Bot'],
    'Together':    ['together-ai', 'TogetherAI'],
    'Fireworks':   ['fireworks-ai', 'FireworksAI'],
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


# Inline A2A Agent Card
# The WorkOS AuthKit AS, from its single origin (workos_authkit). Resolved at
# import like routes/mcp_oauth_2025_06_18.py's _AUTHKIT, so a domain cutover is
# an env change plus a restart, not an edit to this file.
_AK = authkit_endpoints()

# ★2026-09-13 — canon resolves when the card is SERVED, in _a2a_agent_card().
# canon_text() inside the dict below ran once, at import, while every canon
# cache was cold, so the card holds these two as raw templates.
_A2A_DESCRIPTION = "Data center intelligence platform - {canon_facilities} distinct facilities, 1,400+ M&A deals, real-time grid data from 7 ISOs, site scoring, market intelligence across 170+ countries."
_A2A_FACILITY_SEARCH = "Search {canon_facilities} distinct data center facilities worldwide by name, location, provider, or capacity."

A2A_AGENT_CARD = {
    "protocolVersion": "0.2.1",
    "name": "DC Hub Intelligence Agent",
    "description": _A2A_DESCRIPTION,
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
            "description": _A2A_FACILITY_SEARCH,
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
            "description": "Live power grid fuel mix across 5 continents — 7 US ISOs (ERCOT, PJM, CAISO, MISO, SPP, NYISO, ISO-NE) + Great Britain (NESO), ~24 EU bidding zones (ENTSO-E), Taiwan (Taipower), Japan (OCCTO), South Korea (KPX), Brazil (ONS) and Australia (AEMO, partial).",
            "tags": ["power grid", "energy", "fuel mix", "ISO", "real-time"],
            "examples": ["What is ERCOT's current fuel mix?", "Show renewables on PJM grid"]
        },
        {
            "id": "ma-tracking",
            "name": "M&A Transaction Tracking",
            "description": "Track 1,400+ data center M&A deals, CapEx, and investment deals.",
            "tags": ["M&A", "transactions", "deals", "acquisitions", "investment"],
            "examples": ["Recent data center acquisitions", "Deals over $1 billion"]
        },
        {
            "id": "market-intelligence",
            "name": "Market Intelligence",
            "description": "Daily market reports: facility counts, capacity, deal volume, trends across 170+ countries.",
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
        "schemes": [
            {
                "scheme": "apiKey",
                "in": "header",
                "name": "X-API-Key",
                "description": "Optional. Free tier: 100 req/day without key."
            },
            {
                # ADDITIVE enterprise/marketplace path — free tier stays keyless
                # (apiKey scheme above is unchanged). OAuth2 authorization_code via
                # WorkOS AuthKit with RFC 7591 Dynamic Client Registration. Required
                # by Google Cloud Marketplace / Gemini Enterprise Custom-MCP connect.
                "scheme": "oauth2",
                "flow": "authorizationCode",
                "grantType": "authorization_code",
                "issuer": _AK["issuer"],
                "authorizationUrl": _AK["authorizationUrl"],
                "tokenUrl": _AK["tokenUrl"],
                "registrationUrl": _AK["registrationUrl"],
                "scopes": list(AUTHKIT_SCOPES),
                "description": ("Enterprise / marketplace path (Google Cloud "
                                "Marketplace, Gemini Enterprise) via WorkOS AuthKit. "
                                "Additive and OPTIONAL — the free tier stays keyless "
                                "and never requires OAuth.")
            }
        ]
    }
}

def _a2a_agent_card():
    """A2A_AGENT_CARD as served, its canon templates resolved for THIS request."""
    live = {"facility-search": canon_text(_A2A_FACILITY_SEARCH)}
    return {
        **A2A_AGENT_CARD,
        "description": canon_text(_A2A_DESCRIPTION),
        "skills": [{**s, "description": live[s["id"]]} if s["id"] in live else s
                   for s in A2A_AGENT_CARD["skills"]],
    }


# =============================================================================
# BLUEPRINT REGISTRATION
# =============================================================================

# ★ Unregistered — see the note at the end of this file.
discovery_bp = Blueprint('discovery', __name__)


# ----- Google A2A Agent Card -----
@discovery_bp.route('/.well-known/agent.json')
def serve_a2a_agent_card():
    """Serve A2A Agent Card for Google Agent2Agent Protocol discovery"""
    log_ai_access('agent.json')
    response = jsonify(_a2a_agent_card())
    response.headers['Cache-Control'] = 'public, max-age=3600'
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response


# ----- routes deleted 2026-09-21 -----
# ★ This blueprint is NOT registered by main.py, and never was: main.py's
# `discovery_bp` is routes.discovery_routes' (same blueprint name, 'discovery':
# Flask refuses to register both), and this module's own register function was
# never called. Every route below lived here unreachable while tests that
# registered it by hand graded it green — be#4996 fixed /llms-full.txt HERE and
# production kept serving the other copy without the fix. Deleted, with where
# each path is actually served:
#
#   /llms-full.txt             ai_discovery_routes.register_discovery_routes
#   /AGENTS.md, /agents.md     routes/agents_md_fallback.py
#   /.well-known/security.txt  main.py
#   /api/v1/ai-tracking/stats  main.py
#   /api/v1/discovery          main.py
#   /api/v1/ai-tracking/recent never served (404 live) — no other registration
#   /a2a/tasks/send            never served (404 live) — no other registration
#
# tests/test_llms_cite_without_mcp.py fails if a door it covers gains a second
# registration anywhere in the codebase.
