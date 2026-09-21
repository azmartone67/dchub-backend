"""
DC Hub — AI platform identification for agent-traffic attribution.

main.py imports identify_ai_platform() from here; that is the module's only
live use. It used to also define a Flask blueprint serving /AGENTS.md,
/.well-known/agent.json, /llms-full.txt, /.well-known/security.txt, an A2A
task handler and AI-tracking endpoints. main.py never registered it, so none of
those routes was ever reachable, and they were deleted 2026-09-21. The note at
the end of this file says where each path is really served.
"""

import os
import logging
from functools import wraps
from db_utils import get_db

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


# =============================================================================
# DISCOVERY FILE CONTENT
# =============================================================================


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
#   /.well-known/agent.json    routes/agent_a2a.py (+ the CF zone worker, worker.js)
#   /.well-known/security.txt  main.py
#   /api/v1/ai-tracking/stats  main.py
#   /api/v1/discovery          main.py
#   /api/v1/ai-tracking/recent never served (404 live) — no other registration
#   /a2a/tasks/send            never served (404 live) — no other registration
#
# tests/test_llms_cite_without_mcp.py fails if a door it covers gains a second
# registration anywhere in the codebase.
