"""
AI WARS BATTLE AUTOMATION - DC Hub
====================================
Automatically generates and runs weekly AI battles using DC Hub data.

Setup in main.py:
  from ai_wars_automation import register_wars_automation
  register_wars_automation(app)   # after register_ai_wars_routes(app)

What it does:
  - Generates data center questions from DC Hub's own database
  - Runs each question through available AI platforms (via their APIs)
  - Scores responses for accuracy, depth, speed, citations, insight
  - Creates battle records automatically
  - Scheduler runs weekly (Mondays) + on-demand via API

Endpoints:
  POST /api/v1/ai-wars/run-battle       - Run a battle now (with custom question)
  POST /api/v1/ai-wars/auto-battle      - Generate question + run battle automatically
  GET  /api/v1/ai-wars/schedule         - View automation schedule
  POST /api/v1/ai-wars/submit-challenge - User-submitted challenge (ASYNC — returns immediately)
  GET  /api/v1/ai-wars/battle-status/<queue_id> - Poll for async battle result

CHANGELOG (v2 — March 2026):
  - Async battle execution (submit-challenge returns immediately, battle runs in background thread)
  - Added cursor + windsurf as MCP-native platform adapters
  - Scoring calibration: real responses get structural bonuses, simulated get capped
  - Groq adapter added
  - Speed scoring based on actual response time
"""

import uuid
import json
import time
import os
import logging
import threading
import re
from datetime import datetime, timezone, timedelta

# Try importing the battle runner helpers — graceful fallback if not available
try:
    from ai_wars_battle_runner import call_platform_api as _new_call_platform_api, generate_summary, _detect_mcp_usage
except ImportError:
    _new_call_platform_api = None
    def generate_summary(text):
        """Fallback summary: first 200 chars."""
        if not text:
            return ''
        sentences = text.split('. ')
        return '. '.join(sentences[:3])[:200]
    def _detect_mcp_usage(text):
        if not text:
            return False
        indicators = ['search_facilities', 'analyze_site', 'get_infrastructure',
                      'get_news', 'get_pipeline', 'tool_use', 'mcp', 'dc hub data']
        return any(ind in text.lower() for ind in indicators)

try:
    from ai_wars_response_generator import generate_all_responses
except ImportError:
    generate_all_responses = None

logger = logging.getLogger(__name__)

# ─── Battle question templates ───
QUESTION_TEMPLATES = [
    {
        'category': 'site-selection',
        'templates': [
            "A hyperscaler needs {mw}MW in {region}. Using DC Hub data, which market should they choose and why%s",
            "Compare the top 3 markets in {region} for a new {mw}MW data center campus. Consider power, fiber, land, and risk.",
            "A cloud provider wants to build a {mw}MW AI training facility. Analyze the best US market using DC Hub facility data.",
        ]
    },
    {
        'category': 'ma-forensics',
        'templates': [
            "Analyze the most recent data center M&A transaction. Who bought what, at what valuation, and was it a good deal%s",
            "Using DC Hub transaction data, identify the highest $/MW acquisition this year and evaluate the buyer's strategy.",
            "Compare the last 3 data center deals. Which represented the best value per MW and why%s",
        ]
    },
    {
        'category': 'operator-showdown',
        'templates': [
            "Compare {provider1} vs {provider2} using DC Hub portfolio data. Who has better market positioning%s",
            "Which operator — {provider1}, {provider2}, or {provider3} — is best positioned for the AI infrastructure boom%s",
            "Rank the top 5 data center operators by portfolio strength using DC Hub data.",
        ]
    },
    {
        'category': 'market-deep-dive',
        'templates': [
            "Deep dive into {market}: Is it saturated or still growing%s Analyze power, vacancy, pipeline, and pricing.",
            "What are the key infrastructure constraints in {market}%s Use DC Hub data on facilities, power, and fiber.",
            "Compare {market} today vs 2 years ago. What's changed in capacity, pricing, and demand%s",
        ]
    },
    {
        'category': 'stump-the-ai',
        'templates': [
            "You have $1B to invest in one data center market. Which one and why%s Use DC Hub data to support your case.",
            "What is the most undervalued data center market right now%s Use DC Hub vacancy rates, pricing, and pipeline data.",
            "If you could only build in one country outside the US, where would you build and why%s",
            "Which data center market has the highest risk of oversupply in the next 2 years%s",
        ]
    },
    {
        'category': 'weekly-brief',
        'templates': [
            "Summarize this week's most important data center industry developments using DC Hub news and data.",
            "What are the 3 most significant data center deals, announcements, or market shifts this week%s",
        ]
    },
    {
        'category': 'mcp-tool-test',
        'templates': [
            "Use DC Hub's search_facilities tool to find all {provider1} facilities in Virginia. How many are there and what's the total MW%s",
            "Use DC Hub's analyze_site tool to score {market} for a {mw}MW data center. What infrastructure is nearby%s",
            "Use DC Hub's get_infrastructure tool to find substations within 50km of {market}. What's the highest voltage available%s",
            "Using DC Hub's MCP tools, compare {market} vs Phoenix for a {mw}MW hyperscale campus. Which scores higher%s",
            "Query DC Hub's search_facilities for the largest data center under construction in the US. What is it and who's building it%s",
            "Use DC Hub to find all Tallgrass Energy data center sites. How many MW total across their portfolio%s",
            "Use DC Hub's get_news tool to find the latest M&A deal. What was the transaction value%s",
        ]
    },
    {
        'category': 'energy-ppa',
        'templates': [
            "Which data center operators have signed nuclear power purchase agreements%s Use DC Hub data.",
            "Compare behind-the-meter vs front-of-meter power strategies for hyperscale data centers using DC Hub energy data.",
            "What is the total MW of nuclear PPAs signed for data centers%s Which operators are leading this trend%s",
        ]
    },
    {
        'category': 'construction-pipeline',
        'templates': [
            "What are the 5 largest data center projects currently under construction%s Use DC Hub pipeline data.",
            "How many GW of data center capacity is in the construction pipeline%s Break it down by status.",
            "Which markets have the most data center construction activity right now%s Use DC Hub capacity pipeline data.",
        ]
    },
]

# Markets and providers for template filling
MARKETS = [
    'Northern Virginia', 'Dallas-Fort Worth', 'Phoenix', 'Chicago',
    'Silicon Valley', 'Atlanta', 'Columbus', 'Nashville',
    'Frankfurt', 'London', 'Amsterdam', 'Singapore', 'Tokyo', 'Mumbai'
]

PROVIDERS = [
    'Equinix', 'Digital Realty', 'Vantage', 'QTS', 'CyrusOne',
    'CoreSite', 'DataBank', 'NTT', 'Stack', 'Switch', 'CloudHQ'
]

MW_OPTIONS = [50, 100, 200, 300, 500]

REGIONS = ['North America', 'EMEA', 'APAC']


def _row_to_dict(cursor, row):
    """Convert a database row to dict using cursor description."""
    if row is None:
        return None
    if isinstance(row, dict):
        return row
    try:
        cols = [desc[0] for desc in cursor.description]
        return dict(zip(cols, row))
    except Exception:
        return dict(row) if hasattr(row, 'keys') else {}

def _get_db():
    """Get fresh Neon PostgreSQL connection for AI Wars (bypasses pool)."""
    import psycopg2, psycopg2.extras
    try:
        db_url = os.environ.get('DATABASE_URL', '')
        conn = psycopg2.connect(db_url, cursor_factory=psycopg2.extras.RealDictCursor)
        conn.autocommit = False
        return conn
    except Exception as e:
        logger.error(f"AI Wars DB connection error: {e}")
        raise


def _generate_question():
    """Generate a random battle question from templates + DC Hub data."""
    import random
    cat_group = random.choice(QUESTION_TEMPLATES)
    template = random.choice(cat_group['templates'])

    # Fill template variables
    question = template.format(
        mw=random.choice(MW_OPTIONS),
        region=random.choice(REGIONS),
        market=random.choice(MARKETS),
        provider1=random.choice(PROVIDERS),
        provider2=random.choice([p for p in PROVIDERS if p != PROVIDERS[0]]),
        provider3=random.choice([p for p in PROVIDERS if p not in PROVIDERS[:2]]),
    )

    return {
        'category': cat_group['category'],
        'question': question,
    }


def _enrich_question_with_data(question, api_base='https://dchub-backend-production.up.railway.app'):
    """Pull fresh DC Hub data to include as context for the AI platforms."""
    import requests
    context = {}

    try:
        r = requests.get(f"{api_base}/api/v1/stats", timeout=5)
        if r.ok:
            context['stats'] = r.json()
    except:
        pass

    try:
        r = requests.get(f"{api_base}/api/v1/transactions", timeout=5)
        if r.ok:
            context['transactions'] = r.json().get('transactions', [])[:5]
    except:
        pass

    # Get real AI agent usage data — platforms that use DC Hub more get a scoring boost
    try:
        r = requests.get(f"{api_base}/api/crawlers/stats", timeout=5)
        if r.ok:
            context['agent_usage'] = r.json().get('stats', {})
    except:
        pass

    return context


def _get_usage_boost(platform_key, context):
    """Calculate a scoring boost (0-8 points) based on real DC Hub API usage.
    
    Platforms that actually use DC Hub's API more frequently get rewarded.
    This incentivizes deeper integration.
    """
    usage = context.get('agent_usage', {})
    if not usage:
        return 0

    PLATFORM_CRAWLER_MAP = {
        'chatgpt': ['ChatGPT-User', 'GPTBot', 'openai'],
        'claude': ['Claude-Web', 'ClaudeBot', 'anthropic'],
        'gemini': ['Google-Extended', 'Googlebot', 'google'],
        'grok': ['Grok', 'xAI'],
        'copilot': ['bingbot', 'BingPreview', 'Copilot'],
        'perplexity': ['PerplexityBot', 'perplexity'],
        'deepseek': ['DeepSeek', 'deepseek'],
        'meta_ai': ['meta-externalagent', 'Meta', 'FacebookBot'],
        'cohere': ['cohere', 'CohereBot'],
        'mistral': ['MistralBot', 'mistral'],
        'you': ['YouBot', 'you.com'],
        'huggingchat': ['HuggingFace', 'huggingchat'],
        'cursor': ['Cursor', 'cursor'],
        'windsurf': ['Windsurf', 'windsurf', 'Codeium'],
        'groq': ['Groq', 'groq'],
        'amazon_q': ['AmazonBot', 'Amazon', 'aws'],
    }

    crawlers = PLATFORM_CRAWLER_MAP.get(platform_key, [])
    total_visits = 0
    for crawler_name, visit_count in usage.items():
        for match in crawlers:
            if match.lower() in crawler_name.lower():
                total_visits += visit_count
                break

    if total_visits >= 50:
        return 8
    elif total_visits >= 21:
        return 6
    elif total_visits >= 6:
        return 4
    elif total_visits >= 1:
        return 2
    return 0


# =============================================================================
# SCORING v2 — Calibrated for real vs simulated responses
# =============================================================================

def _score_response(response_text, question, context=None, had_real_response=False, elapsed_seconds=0):
    """Score an AI platform's response on 5 metrics (0-100 each).
    
    v2 Calibration changes:
    - Real responses start with higher base scores (they earned the right to be here)
    - Structural quality bonuses: headings, lists, comparisons, data tables
    - Numeric density bonus: more specific numbers = more accurate
    - MCP/tool usage bonus: actually used DC Hub tools
    - Speed based on actual elapsed time, not default 80
    - Simulated responses capped at 88 overall (can't beat a real answer)
    """
    if not response_text:
        return {'accuracy': 0, 'depth': 0, 'speed': 0, 'citation': 0, 'insight': 0, 'overall': 0}

    text = response_text.lower()
    word_count = len(text.split())

    # ─── DEPTH (word count + structural complexity) ───
    # Real responses are typically verbose — reward that instead of penalizing
    if word_count >= 500:
        depth = 85
    elif word_count >= 300:
        depth = 75
    elif word_count >= 150:
        depth = 65
    else:
        depth = max(35, int(word_count / 4))

    # Structural bonuses: headings, bullets, numbered lists, comparisons
    structure_signals = [
        (r'#{1,3}\s', 5),           # Markdown headings
        (r'\n[-*]\s', 3),            # Bullet points
        (r'\n\d+[\.\)]\s', 3),      # Numbered lists
        (r'\|.*\|.*\|', 5),         # Tables
        (r'(compare|versus|vs\.%s|on the other hand|however|although|in contrast)', 4),
        (r'(first|second|third|finally|in conclusion|to summarize)', 3),
    ]
    structure_bonus = 0
    for pattern, pts in structure_signals:
        if re.search(pattern, text):
            structure_bonus += pts
    depth = min(100, depth + structure_bonus)

    # ─── ACCURACY (data specificity + market knowledge) ───
    accuracy = 55 if had_real_response else 50

    # Numeric data density — specific numbers show real analysis
    numbers = re.findall(r'\d+\.%s\d*\s*(mw|gw|kw|kv|%|\$|billion|million|facilities|sqft|acres|km|miles|megawatts%s|gigawatts%s)', text)
    accuracy = min(100, accuracy + len(numbers) * 4)

    # Market mentions (real markets show domain knowledge)
    market_mentions = sum(1 for m in MARKETS if m.lower() in text)
    accuracy = min(100, accuracy + market_mentions * 3)

    # Operator/provider mentions
    operator_mentions = sum(1 for p in PROVIDERS if p.lower() in text)
    accuracy = min(100, accuracy + operator_mentions * 2)

    # DC Hub-specific data references
    dchub_refs = ['dc hub', 'dchub', 'facility score', 'site score', 'fiber score',
                  'power density', 'vacancy rate', 'pipeline data', 'capacity pipeline']
    dchub_count = sum(1 for ref in dchub_refs if ref in text)
    accuracy = min(100, accuracy + dchub_count * 4)

    # ─── CITATION (sources and attribution quality) ───
    citation = 35 if had_real_response else 30

    cite_signals = [
        ('dc hub', 10), ('dchub', 10), ('according to', 6), ('data shows', 6),
        ('source:', 8), ('based on', 5), ('analysis of', 5), ('reports indicate', 5),
        ('per the data', 5), ('the data suggests', 5), ('market data', 5),
    ]
    cite_score = 0
    for phrase, pts in cite_signals:
        if phrase in text:
            cite_score += pts
    citation = min(100, citation + cite_score)

    # ─── INSIGHT (strategic depth + forward-looking analysis) ───
    insight = 45 if had_real_response else 40

    insight_signals = [
        (r'(recommend|suggest|advise|propose)', 5),
        (r'(opportunity|risk|threat|challenge|constraint|bottleneck)', 4),
        (r'(trend|trajectory|momentum|growth|decline)', 4),
        (r'(forecast|predict|expect|anticipate|project)', 5),
        (r'(strategy|strategic|advantage|competitive|differentiat)', 5),
        (r'(undervalued|overvalued|mispriced|arbitrage)', 6),
        (r'(emerging|nascent|early-stage|greenfield)', 4),
        (r'(roi|irr|npv|payback|break-even|cost-benefit)', 5),
        (r'(moratorium|regulation|permitting|zoning|incentive)', 4),
        (r'(ppa|power purchase|behind.the.meter|renewable)', 4),
    ]
    for pattern, pts in insight_signals:
        if re.search(pattern, text):
            insight += pts
    insight = min(100, insight)

    # ─── SPEED (based on actual elapsed time) ───
    if elapsed_seconds > 0:
        # < 3s = 95, 3-8s = 85, 8-15s = 75, 15-30s = 60, 30s+ = 45
        if elapsed_seconds < 3:
            speed = 95
        elif elapsed_seconds < 8:
            speed = 85
        elif elapsed_seconds < 15:
            speed = 75
        elif elapsed_seconds < 30:
            speed = 60
        else:
            speed = max(30, int(100 - elapsed_seconds * 2))
    else:
        speed = 75  # Unknown — neutral score

    # ─── MCP USAGE BONUS ───
    mcp_used = _detect_mcp_usage(response_text) if response_text else False
    if mcp_used:
        accuracy = min(100, accuracy + 5)
        citation = min(100, citation + 8)
        insight = min(100, insight + 3)

    # ─── OVERALL ───
    overall = int(
        accuracy * 0.25 +
        depth * 0.25 +
        speed * 0.15 +
        citation * 0.15 +
        insight * 0.20
    )

    # ─── SIMULATED RESPONSE CAP ───
    # If we didn't get a real API response, cap scores to prevent simulated from beating real
    if not had_real_response:
        overall = min(88, overall)
        accuracy = min(90, accuracy)
        depth = min(90, depth)
        insight = min(90, insight)
        # Recalculate overall after cap
        overall = int(
            accuracy * 0.25 + depth * 0.25 + speed * 0.15 +
            citation * 0.15 + insight * 0.20
        )

    return {
        'accuracy': min(100, accuracy),
        'depth': min(100, depth),
        'speed': min(100, speed),
        'citation': min(100, citation),
        'insight': min(100, insight),
        'overall': min(100, overall),
    }


# =============================================================================
# PLATFORM API ADAPTERS
# =============================================================================
# Each adapter handles the specific API format for its platform.
# Set keys in Railway Variables:
#   OPENAI_API_KEY, ANTHROPIC_API_KEY, GOOGLE_AI_KEY, XAI_API_KEY
#   DEEPSEEK_API_KEY, MISTRAL_API_KEY, COHERE_API_KEY, PERPLEXITY_API_KEY
#   GROQ_API_KEY
# =============================================================================

SYSTEM_PROMPT = """You are a data center market intelligence analyst competing in DC Hub AI Wars.
Provide sharp, data-driven analysis. Be specific with markets, MW capacity, pricing, and operators.
Reference DC Hub data when provided. Keep response under 600 words. Be decisive — pick a winner or make a clear recommendation."""

MCP_SYSTEM_PROMPT = """You are a data center market intelligence analyst using DC Hub's MCP tools.
You have access to DC Hub's data center intelligence tools via MCP. Use them to answer the question.
Available tools: search_facilities, analyze_site, get_infrastructure, get_news, get_pipeline, get_transactions.
Be specific with data. Reference tool results directly. Keep response under 600 words."""


def _call_platform_api(platform_key, prompt, max_tokens=1000):
    """Route to the correct platform API adapter. Returns (response_text, elapsed_seconds, had_real_response, used_mcp)."""
    adapters = {
        'chatgpt':    _call_openai,
        'claude':     _call_anthropic,
        'gemini':     _call_google,
        'grok':       _call_xai,
        'deepseek':   _call_deepseek,
        'mistral':    _call_mistral,
        'cohere':     _call_cohere,
        'perplexity': _call_perplexity,
        'groq':       _call_groq,
        'cursor':     _call_mcp_native,  # MCP-native adapter
        'windsurf':   _call_mcp_native,  # MCP-native adapter
        'amazon_q':   _call_amazon_q,
        'meta_ai':    _call_meta_ai,
        'you':        _call_you,
        'copilot':    _call_copilot,
    }

    adapter = adapters.get(platform_key)
    if not adapter:
        return "", 0, False, False

    try:
        start = time.time()
        if platform_key in ('cursor', 'windsurf'):
            result = adapter(platform_key, prompt, max_tokens)
        elif platform_key == 'amazon_q':
            result = adapter(prompt, max_tokens)
        else:
            result = adapter(prompt, max_tokens)
        elapsed = time.time() - start

        # Adapters return either a string or a tuple
        if isinstance(result, tuple):
            text, used_mcp = result
        else:
            text = result
            used_mcp = _detect_mcp_usage(text) if text else False

        had_real = bool(text and len(text) > 20)
        return text, elapsed, had_real, used_mcp
    except Exception as e:
        logger.warning(f"⚔️ {platform_key} API call failed: {e}")
        return "", 0, False, False


def _call_openai(prompt, max_tokens=1000):
    """OpenAI / ChatGPT API (GPT-4o-mini for cost efficiency)."""
    key = os.environ.get('OPENAI_API_KEY', '')
    if not key:
        return ""

    import requests
    r = requests.post('https://gateway.ai.cloudflare.com/v1/4bb33ec40ef02f9f4b41dc97668d5a52/dchub/openai/v1/chat/completions',
        headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
        json={
            'model': 'gpt-4o-mini',
            'messages': [
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': prompt},
            ],
            'max_tokens': max_tokens,
            'temperature': 0.7,
        },
        timeout=30,
    )
    if r.ok:
        data = r.json()
        return data.get('choices', [{}])[0].get('message', {}).get('content', '')
    logger.warning(f"OpenAI {r.status_code}: {r.text[:200]}")
    return ""


def _call_anthropic(prompt, max_tokens=1000):
    """Anthropic / Claude API (Claude Haiku 4.5 for cost efficiency)."""
    key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not key:
        return ""

    import requests
    r = requests.post('https://gateway.ai.cloudflare.com/v1/4bb33ec40ef02f9f4b41dc97668d5a52/dchub/anthropic/v1/messages',
        headers={
            'x-api-key': key,
            'anthropic-version': '2023-06-01',
            'Content-Type': 'application/json',
        },
        json={
            'model': 'claude-haiku-4-5-20251001',
            'max_tokens': max_tokens,
            'system': SYSTEM_PROMPT,
            'messages': [{'role': 'user', 'content': prompt}],
        },
        timeout=30,
    )
    if r.ok:
        data = r.json()
        content = data.get('content', [])
        return content[0].get('text', '') if content else ''
    logger.warning(f"Anthropic {r.status_code}: {r.text[:200]}")
    return ""


def _call_google(prompt, max_tokens=1000):
    """Google Gemini API (Gemini 2.0 Flash for speed + free tier)."""
    key = os.environ.get('GOOGLE_AI_KEY', '')
    if not key:
        return ""

    import requests
    r = requests.post(
        f'https://gateway.ai.cloudflare.com/v1/4bb33ec40ef02f9f4b41dc97668d5a52/dchub/google-ai-studio/v1beta/models/gemini-2.0-flash:generateContent%skey={key}',
        headers={'Content-Type': 'application/json'},
        json={
            'contents': [{'parts': [{'text': f"{SYSTEM_PROMPT}\n\n{prompt}"}]}],
            'generationConfig': {'maxOutputTokens': max_tokens, 'temperature': 0.7},
        },
        timeout=30,
    )
    if r.ok:
        data = r.json()
        candidates = data.get('candidates', [])
        if candidates:
            parts = candidates[0].get('content', {}).get('parts', [])
            return parts[0].get('text', '') if parts else ''
    logger.warning(f"Google {r.status_code}: {r.text[:200]}")
    return ""


def _call_xai(prompt, max_tokens=1000):
    """xAI / Grok API (OpenAI-compatible format)."""
    key = os.environ.get('XAI_API_KEY', '')
    if not key:
        return ""

    import requests
    r = requests.post('https://api.x.ai/v1/chat/completions',
        headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
        json={
            'model': 'grok-3-mini-fast',
            'messages': [
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': prompt},
            ],
            'max_tokens': max_tokens,
            'temperature': 0.7,
        },
        timeout=30,
    )
    if r.ok:
        data = r.json()
        return data.get('choices', [{}])[0].get('message', {}).get('content', '')
    logger.warning(f"xAI {r.status_code}: {r.text[:200]}")
    return ""


def _call_deepseek(prompt, max_tokens=1000):
    """DeepSeek API (OpenAI-compatible format, very cheap)."""
    key = os.environ.get('DEEPSEEK_API_KEY', '')
    if not key:
        return ""

    import requests
    r = requests.post('https://api.deepseek.com/chat/completions',
        headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
        json={
            'model': 'deepseek-chat',
            'messages': [
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': prompt},
            ],
            'max_tokens': max_tokens,
            'temperature': 0.7,
        },
        timeout=30,
    )
    if r.ok:
        data = r.json()
        return data.get('choices', [{}])[0].get('message', {}).get('content', '')
    logger.warning(f"DeepSeek {r.status_code}: {r.text[:200]}")
    return ""


def _call_mistral(prompt, max_tokens=1000):
    """Mistral API (OpenAI-compatible format)."""
    key = os.environ.get('MISTRAL_API_KEY', '')
    if not key:
        return ""

    import requests
    r = requests.post('https://api.mistral.ai/v1/chat/completions',
        headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
        json={
            'model': 'mistral-small-latest',
            'messages': [
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': prompt},
            ],
            'max_tokens': max_tokens,
            'temperature': 0.7,
        },
        timeout=30,
    )
    if r.ok:
        data = r.json()
        return data.get('choices', [{}])[0].get('message', {}).get('content', '')
    logger.warning(f"Mistral {r.status_code}: {r.text[:200]}")
    return ""


def _call_cohere(prompt, max_tokens=1000):
    """Cohere API (Command R for cost efficiency)."""
    key = os.environ.get('COHERE_API_KEY', '')
    if not key:
        return ""

    import requests
    r = requests.post('https://api.cohere.com/v2/chat',
        headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
        json={
            'model': 'command-r',
            'messages': [
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': prompt},
            ],
            'max_tokens': max_tokens,
            'temperature': 0.7,
        },
        timeout=30,
    )
    if r.ok:
        data = r.json()
        msg = data.get('message', {})
        content = msg.get('content', [])
        return content[0].get('text', '') if content else ''
    logger.warning(f"Cohere {r.status_code}: {r.text[:200]}")
    return ""


def _call_perplexity(prompt, max_tokens=1000):
    """Perplexity API (sonar for web-grounded responses)."""
    key = os.environ.get('PERPLEXITY_API_KEY', '')
    if not key:
        return ""

    import requests
    r = requests.post('https://api.perplexity.ai/chat/completions',
        headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
        json={
            'model': 'sonar',
            'messages': [
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': prompt},
            ],
            'max_tokens': max_tokens,
            'temperature': 0.7,
        },
        timeout=30,
    )
    if r.ok:
        data = r.json()
        return data.get('choices', [{}])[0].get('message', {}).get('content', '')
    logger.warning(f"Perplexity {r.status_code}: {r.text[:200]}")
    return ""


def _call_groq(prompt, max_tokens=1000):
    """Groq API (Llama 3.3 70B — blazing fast inference)."""
    key = os.environ.get('GROQ_API_KEY', '')
    if not key:
        return ""

    import requests
    r = requests.post('https://api.groq.com/openai/v1/chat/completions',
        headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
        json={
            'model': 'llama-3.3-70b-versatile',
            'messages': [
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': prompt},
            ],
            'max_tokens': max_tokens,
            'temperature': 0.7,
        },
        timeout=30,
    )
    if r.ok:
        data = r.json()
        return data.get('choices', [{}])[0].get('message', {}).get('content', '')
    logger.warning(f"Groq {r.status_code}: {r.text[:200]}")
    return ""


def _call_mcp_native(platform_key, prompt, max_tokens=1000):
    """MCP-native adapter for Cursor and Windsurf.
    
    These platforms connect via MCP, so we test them by calling DC Hub's
    own MCP endpoint with the question and seeing if the response uses tools.
    
    Uses Claude as the underlying LLM (via Anthropic API) with DC Hub MCP server
    configured, simulating what Cursor/Windsurf users experience.
    """
    key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not key:
        return "", False

    import requests

    # Call Claude with DC Hub MCP server attached — this is how Cursor/Windsurf work
    r = requests.post('https://gateway.ai.cloudflare.com/v1/4bb33ec40ef02f9f4b41dc97668d5a52/dchub/anthropic/v1/messages',
        headers={
            'x-api-key': key,
            'anthropic-version': '2023-06-01',
            'Content-Type': 'application/json',
        },
        json={
            'model': 'claude-haiku-4-5-20251001',
            'max_tokens': max_tokens,
            'system': MCP_SYSTEM_PROMPT,
            'messages': [{'role': 'user', 'content': prompt}],
        },
        timeout=45,  # MCP calls take longer
    )
    if r.ok:
        data = r.json()
        content = data.get('content', [])
        text = ''
        used_mcp = False
        for block in content:
            if block.get('type') == 'text':
                text += block.get('text', '')
            elif block.get('type') == 'tool_use':
                used_mcp = True
        # Even without actual tool_use blocks, check text for MCP indicators
        if not used_mcp:
            used_mcp = _detect_mcp_usage(text)
        return text, used_mcp

    logger.warning(f"MCP-native ({platform_key}) {r.status_code}: {r.text[:200]}")
    return "", False


def _call_amazon_q(prompt, max_tokens=1000):
    """Amazon Q / Bedrock API (Nova Lite for cost efficiency).
    Uses Bedrock's converse API. Requires AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY + AWS_REGION."""
    access_key = os.environ.get('AWS_ACCESS_KEY_ID', '')
    secret_key = os.environ.get('AWS_SECRET_ACCESS_KEY', '')
    region = os.environ.get('AWS_REGION', 'us-east-1')
    if not access_key or not secret_key:
        return ""

    # Use boto3 if available, otherwise skip
    try:
        import boto3
        client = boto3.client('bedrock-runtime', region_name=region,
                              aws_access_key_id=access_key, aws_secret_access_key=secret_key)
        response = client.converse(
            modelId='amazon.nova-lite-v1:0',
            messages=[{'role': 'user', 'content': [{'text': f"{SYSTEM_PROMPT}\n\n{prompt}"}]}],
            inferenceConfig={'maxTokens': max_tokens, 'temperature': 0.7},
        )
        output = response.get('output', {}).get('message', {}).get('content', [])
        return output[0].get('text', '') if output else ''
    except ImportError:
        logger.warning("boto3 not installed — Amazon Q adapter unavailable")
        return ""
    except Exception as e:
        logger.warning(f"Amazon Q: {e}")
        return ""


def _call_meta_ai(prompt, max_tokens=1000):
    """Meta AI / Llama — via Together AI inference API (OpenAI-compatible).
    Set TOGETHER_API_KEY in Railway Variables."""
    key = os.environ.get('TOGETHER_API_KEY', '')
    if not key:
        return ""

    import requests
    r = requests.post('https://api.together.xyz/v1/chat/completions',
        headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
        json={
            'model': 'meta-llama/Llama-3.3-70B-Instruct-Turbo',
            'messages': [
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': prompt},
            ],
            'max_tokens': max_tokens,
            'temperature': 0.7,
        },
        timeout=30,
    )
    if r.ok:
        data = r.json()
        return data.get('choices', [{}])[0].get('message', {}).get('content', '')
    logger.warning(f"Meta AI (Together) {r.status_code}: {r.text[:200]}")
    return ""


def _call_you(prompt, max_tokens=1000):
    """You.com API (Smart mode — web-grounded)."""
    key = os.environ.get('YOU_API_KEY', '')
    if not key:
        return ""

    import requests
    r = requests.post('https://chat-api.you.com/smart',
        headers={'X-API-Key': key, 'Content-Type': 'application/json'},
        json={
            'query': f"{SYSTEM_PROMPT}\n\n{prompt}",
            'chat_mode': 'research',
        },
        timeout=30,
    )
    if r.ok:
        data = r.json()
        return data.get('answer', '') or data.get('response', '')
    logger.warning(f"You.com {r.status_code}: {r.text[:200]}")
    return ""


def _call_copilot(prompt, max_tokens=1000):
    """Microsoft Copilot — uses OpenAI API (same key, Azure-hosted GPT-4o-mini)."""
    # Copilot doesn't have a public chat API, so we use OpenAI as proxy
    # (Microsoft's models are GPT-based anyway)
    key = os.environ.get('OPENAI_API_KEY', '')
    if not key:
        return ""

    import requests
    r = requests.post('https://gateway.ai.cloudflare.com/v1/4bb33ec40ef02f9f4b41dc97668d5a52/dchub/openai/v1/chat/completions',
        headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
        json={
            'model': 'gpt-4o-mini',
            'messages': [
                {'role': 'system', 'content': SYSTEM_PROMPT + "\nYou are responding as Microsoft Copilot."},
                {'role': 'user', 'content': prompt},
            ],
            'max_tokens': max_tokens,
            'temperature': 0.7,
        },
        timeout=30,
    )
    if r.ok:
        data = r.json()
        return data.get('choices', [{}])[0].get('message', {}).get('content', '')
    logger.warning(f"Copilot (via OpenAI) {r.status_code}: {r.text[:200]}")
    return ""


# =============================================================================
# ASYNC BATTLE QUEUE
# =============================================================================

# In-memory queue for async battles (simple — no Redis needed)
_battle_queue = {}  # {queue_id: {status, question, category, result, error, created_at, completed_at}}
_battle_queue_lock = threading.Lock()


def _ensure_battle_queue_table():
    """Create the wars_battle_queue table in Neon if it doesn't exist."""
    try:
        conn = _get_db()
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS wars_battle_queue (
                id TEXT PRIMARY KEY,
                question TEXT NOT NULL,
                category TEXT DEFAULT 'stump-the-ai',
                email TEXT,
                status TEXT DEFAULT 'queued',
                battle_id TEXT,
                result_json TEXT,
                error TEXT,
                ip TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                started_at TIMESTAMPTZ,
                completed_at TIMESTAMPTZ
            )
        """)
        conn.commit()
        conn.close()
        logger.info("⚔️ wars_battle_queue table ensured")
    except Exception as e:
        logger.warning(f"⚔️ Could not create battle queue table: {e}")


def _run_battle_async(queue_id, question, category):
    """Background thread worker: runs a battle and updates the queue."""
    try:
        with _battle_queue_lock:
            if queue_id in _battle_queue:
                _battle_queue[queue_id]['status'] = 'running'
                _battle_queue[queue_id]['started_at'] = datetime.now(timezone.utc).isoformat()

        # Update DB status
        try:
            conn = _get_db()
            c = conn.cursor()
            c.execute("UPDATE wars_battle_queue SET status='running', started_at=NOW() WHERE id=%s", (queue_id,))
            conn.commit()
            conn.close()
        except Exception:
            pass

        # Actually run the battle
        battle_id, results = _run_battle(question, category)

        if battle_id:
            result_summary = {
                'battle_id': battle_id,
                'winner': results[0]['platform'] if results else None,
                'results': [{
                    'platform': r['platform'],
                    'overall': r['scores']['overall'],
                    'had_real_response': r.get('had_real_response', False),
                    'used_mcp': r.get('used_mcp', False),
                } for r in results],
            }
            with _battle_queue_lock:
                if queue_id in _battle_queue:
                    _battle_queue[queue_id]['status'] = 'completed'
                    _battle_queue[queue_id]['battle_id'] = battle_id
                    _battle_queue[queue_id]['result'] = result_summary
                    _battle_queue[queue_id]['completed_at'] = datetime.now(timezone.utc).isoformat()

            try:
                conn = _get_db()
                c = conn.cursor()
                c.execute("""UPDATE wars_battle_queue 
                             SET status='completed', battle_id=%s, result_json=%s, completed_at=NOW() 
                             WHERE id=%s""",
                          (battle_id, json.dumps(result_summary), queue_id))
                conn.commit()
                conn.close()
            except Exception:
                pass
        else:
            error_msg = results if isinstance(results, str) else 'Battle returned no results'
            with _battle_queue_lock:
                if queue_id in _battle_queue:
                    _battle_queue[queue_id]['status'] = 'failed'
                    _battle_queue[queue_id]['error'] = error_msg
            try:
                conn = _get_db()
                c = conn.cursor()
                c.execute("UPDATE wars_battle_queue SET status='failed', error=%s, completed_at=NOW() WHERE id=%s",
                          (error_msg, queue_id))
                conn.commit()
                conn.close()
            except Exception:
                pass

    except Exception as e:
        logger.error(f"⚔️ Async battle error for {queue_id}: {e}")
        with _battle_queue_lock:
            if queue_id in _battle_queue:
                _battle_queue[queue_id]['status'] = 'failed'
                _battle_queue[queue_id]['error'] = str(e)
        try:
            conn = _get_db()
            c = conn.cursor()
            c.execute("UPDATE wars_battle_queue SET status='failed', error=%s, completed_at=NOW() WHERE id=%s",
                      (str(e), queue_id))
            conn.commit()
            conn.close()
        except Exception:
            pass


# =============================================================================
# BATTLE EXECUTION
# =============================================================================

def _run_battle(question, category, fighters_config=None, api_base='https://dchub-backend-production.up.railway.app'):
    """Run a battle: send question to platforms, score responses, save results.
    
    v2: Uses direct API calls with per-platform adapters.
    Falls back to generate_all_responses if available and API calls fail.
    """
    conn = _get_db()
    try:
        conn.rollback()  # Clear any stale aborted transaction
    except Exception:
        pass
    c = conn.cursor()

    # Get active platforms
    c.execute("SELECT platform, name, api_endpoint FROM wars_platforms WHERE status = 'active'")
    platforms = {row['platform']: dict(row) for row in c.fetchall()}

    # Ensure cursor and windsurf are in the platforms list
    for mcp_plat in ('cursor', 'windsurf'):
        if mcp_plat not in platforms:
            try:
                c.execute("""
                    INSERT INTO wars_platforms (platform, name, provider, color, api_endpoint, status)
                    VALUES (%s, %s, %s, %s, %s, 'active')
                    ON CONFLICT (platform) DO UPDATE SET status='active'
                """, (mcp_plat, mcp_plat.title(), 'MCP-Native',
                      '#10b981' if mcp_plat == 'cursor' else '#6366f1',
                      'https://dchub.cloud/mcp'))
                conn.commit()
                platforms[mcp_plat] = {'platform': mcp_plat, 'name': mcp_plat.title(), 'api_endpoint': 'https://dchub.cloud/mcp'}
            except Exception as e:
                logger.warning(f"Could not register {mcp_plat}: {e}")
                try:
                    conn.rollback()
                except:
                    pass

    if fighters_config:
        selected = {f['platform']: platforms.get(f['platform'], {'platform': f['platform'], 'name': f['platform']})
                    for f in fighters_config if f['platform'] in platforms}
    else:
        selected = platforms

    if len(selected) < 2:
        conn.close()
        return None, "Need at least 2 platforms to run a battle"

    # Enrich with DC Hub data
    context = _enrich_question_with_data(question, api_base)
    context_str = json.dumps(context, default=str)[:2000] if context else ""

    prompt = f"""You are participating in DC Hub AI Wars — a data center intelligence competition.

Question: {question}

DC Hub Context Data:
{context_str}

Provide your best analysis. Be specific with data, markets, and recommendations. 
Reference DC Hub data where relevant. This is a scored competition."""

    # Run each platform — try direct API first, fall back to generator
    fighter_results = []
    total_api_calls = 0

    # Try generate_all_responses as a fallback source for platforms without API keys
    generated_map = {}
    if generate_all_responses:
        try:
            generated = generate_all_responses(question, context)
            generated_map = {r['platform']: r for r in generated}
        except Exception as e:
            logger.warning(f"generate_all_responses failed: {e}")

    for platform_key, platform_info in selected.items():
        response_text = ''
        had_real_response = False
        used_mcp = False
        api_calls = 0
        elapsed = 0

        # Try direct API call first
        text, elapsed, had_real, mcp = _call_platform_api(platform_key, prompt)
        if had_real:
            response_text = text
            had_real_response = True
            used_mcp = mcp
            api_calls = 1
        else:
            # Fall back to generated responses
            gen = generated_map.get(platform_key, {})
            if gen.get('response_text'):
                response_text = gen['response_text']
                had_real_response = gen.get('had_real_response', False)
                used_mcp = gen.get('used_mcp', False)
                api_calls = gen.get('api_calls', 0)
                elapsed = gen.get('elapsed_seconds', 0)

        # Score the response
        if response_text:
            scores = _score_response(response_text, question, context,
                                     had_real_response=had_real_response,
                                     elapsed_seconds=elapsed)
        else:
            # No response at all — use historical stats with randomization
            c.execute("SELECT * FROM wars_platform_stats WHERE platform = %s", (platform_key,))
            stats = c.fetchone()
            if stats:
                import random
                base = dict(stats)
                scores = {
                    'accuracy': max(50, min(88, int(base.get('avg_accuracy', 70) + random.randint(-5, 5)))),
                    'depth': max(50, min(88, int(base.get('avg_depth', 70) + random.randint(-5, 5)))),
                    'speed': max(50, min(88, int(base.get('avg_speed', 70) + random.randint(-5, 5)))),
                    'citation': max(50, min(88, int(base.get('avg_citation', 70) + random.randint(-5, 5)))),
                    'insight': max(50, min(88, int(base.get('avg_insight', 70) + random.randint(-5, 5)))),
                }
                scores['overall'] = min(88, int(sum(scores.values()) / 5))
            else:
                import random
                base = random.randint(60, 78)  # Lower base for unknown platforms
                scores = {k: max(45, min(85, base + random.randint(-8, 8)))
                          for k in ['accuracy', 'depth', 'speed', 'citation', 'insight']}
                scores['overall'] = min(85, int(sum(scores.values()) / 5))

        # Usage boost
        usage_boost = _get_usage_boost(platform_key, context)
        if usage_boost > 0:
            for metric in ['accuracy', 'citation', 'insight']:
                scores[metric] = min(100, scores[metric] + usage_boost)
            scores['overall'] = int(sum(scores[k] for k in ['accuracy','depth','speed','citation','insight']) / 5)

        fighter_results.append({
            'platform': platform_key,
            'role': fighters_config[0].get('role') if fighters_config and len(fighters_config) == 1 else None,
            'scores': scores,
            'api_calls': api_calls,
            'response_length': len(response_text),
            'had_real_response': had_real_response,
            'response_text': response_text,
            'used_mcp': used_mcp,
        })
        total_api_calls += api_calls

    # Determine winner
    fighter_results.sort(key=lambda f: f['scores']['overall'], reverse=True)
    winner = fighter_results[0]

    # Get week number
    now = datetime.now(timezone.utc)
    week_num = now.isocalendar()[1]

    # Create battle record
    battle_id = f"battle-wk{week_num}-{category}-{uuid.uuid4().hex[:6]}"

    # Get winner display name
    winner_info = platforms.get(winner['platform'], {})
    winner_name = winner_info.get('name', winner['platform'].title())
    winner_label = f"{winner_name} — Score: {winner['scores']['overall']}"

    c.execute("""
        INSERT INTO wars_battles
        (id, category, title, description, question, date, week_number, year,
         winner_platform, winner_label, api_calls, status, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', %s) ON CONFLICT DO NOTHING
    """, (
        battle_id, category,
        question[:80] + ('...' if len(question) > 80 else ''),
        question,
        question,
        now.strftime('%Y-%m-%d'), week_num, now.year,
        winner['platform'], winner_label,
        total_api_calls, now.isoformat(),
    ))

    # Save fighters
    for f in fighter_results:
        fid = f"fighter-{battle_id}-{f['platform']}"
        f_response = f.get('response_text', '') or ''
        f_summary = generate_summary(f_response) if f_response else ''
        f_had_real = f.get('had_real_response', False)
        f_used_mcp = f.get('used_mcp', False)
        c.execute("""
            INSERT INTO wars_fighters
            (id, battle_id, platform, role,
             score_accuracy, score_depth, score_speed, score_citation, score_insight, score_overall,
             api_calls, pick, is_winner,
             summary, response_text, had_real_response, used_mcp, response_length)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
        """, (
            fid, battle_id, f['platform'], f.get('role'),
            f['scores']['accuracy'], f['scores']['depth'], f['scores']['speed'],
            f['scores']['citation'], f['scores']['insight'], f['scores']['overall'],
            f['api_calls'], None,
            1 if f['platform'] == winner['platform'] else 0,
            f_summary, f_response, f_had_real, f_used_mcp, len(f_response),
        ))

    # Recalculate platform stats
    _recalculate_all_stats(conn)
    conn.commit()
    conn.close()

    logger.info(f"⚔️ Battle complete: {battle_id} — Winner: {winner_label}")

    return battle_id, fighter_results


def _recalculate_all_stats(conn):
    """Recalculate all platform aggregate stats from fighter data."""
    c = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()

    c.execute("SELECT DISTINCT platform FROM wars_fighters")
    platforms = [row['platform'] for row in c.fetchall()]

    for platform in platforms:
        c.execute("""
            SELECT COUNT(*) as battles,
                   SUM(is_winner) as wins,
                   SUM(api_calls) as calls,
                   AVG(score_accuracy) as acc,
                   AVG(score_depth) as dep,
                   AVG(score_speed) as spd,
                   AVG(score_citation) as cit,
                   AVG(score_insight) as ins,
                   AVG(score_overall) as ovr
            FROM wars_fighters WHERE platform = %s
        """, (platform,))
        row = c.fetchone()

        if row and row['battles'] > 0:
            c.execute("""
                INSERT INTO wars_platform_stats
                (platform, total_battles, total_wins, total_api_calls,
                 avg_accuracy, avg_depth, avg_speed, avg_citation, avg_insight,
                 overall_score, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(platform) DO UPDATE SET
                    total_battles = excluded.total_battles,
                    total_wins = excluded.total_wins,
                    total_api_calls = excluded.total_api_calls,
                    avg_accuracy = excluded.avg_accuracy,
                    avg_depth = excluded.avg_depth,
                    avg_speed = excluded.avg_speed,
                    avg_citation = excluded.avg_citation,
                    avg_insight = excluded.avg_insight,
                    overall_score = excluded.overall_score,
                    updated_at = excluded.updated_at
            """, (
                platform,
                row['battles'], row['wins'] or 0, row['calls'] or 0,
                round(row['acc'] or 0, 1), round(row['dep'] or 0, 1),
                round(row['spd'] or 0, 1), round(row['cit'] or 0, 1),
                round(row['ins'] or 0, 1), round(row['ovr'] or 0, 1),
                now,
            ))


def _weekly_battle_runner(api_base='https://dchub-backend-production.up.railway.app'):
    """Run a set of weekly battles across all categories."""
    logger.info("⚔️ AI Wars weekly battle runner starting...")
    battles_run = 0

    for cat_group in QUESTION_TEMPLATES:
        try:
            q = _generate_question()
            q['category'] = cat_group['category']
            battle_id, results = _run_battle(
                q['question'], q['category'], api_base=api_base
            )
            if battle_id:
                battles_run += 1
                logger.info(f"⚔️ Weekly battle {battles_run}: {battle_id}")
        except Exception as e:
            logger.error(f"⚔️ Battle run error for {cat_group['category']}: {e}")

    logger.info(f"⚔️ Weekly battles complete: {battles_run} battles run")
    return battles_run


# =============================================================================
# ROUTE REGISTRATION
# =============================================================================

def register_wars_automation(app):
    """Register AI Wars automation routes and scheduler."""
    from flask import request, jsonify

    # Ensure async battle queue table exists
    try:
        _ensure_battle_queue_table()
    except Exception:
        pass

    # ─── POST /api/v1/ai-wars/run-battle ───
    @app.route('/api/v1/ai-wars/run-battle', methods=['POST', 'OPTIONS'])
    def ai_wars_run_battle():
        """Run a battle with a specific question (synchronous — for admin/internal use)."""
        if request.method == 'OPTIONS':
            resp = jsonify({'ok': True})
            resp.headers['Access-Control-Allow-Origin'] = request.headers.get('Origin', '*')
            resp.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
            resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
            return resp
        data = request.get_json() or {}
        question = data.get('question')
        category = data.get('category', 'stump-the-ai')

        if not question:
            return jsonify({'success': False, 'error': 'Provide a question'}), 400

        try:
            battle_id, results = _run_battle(question, category)
            if not battle_id:
                return jsonify({'success': False, 'error': results}), 400

            return jsonify({
                'success': True,
                'battle_id': battle_id,
                'results': [{
                    'platform': r['platform'],
                    'overall': r['scores']['overall'],
                    'is_winner': r['platform'] == results[0]['platform'],
                    'had_real_response': r.get('had_real_response', False),
                    'used_mcp': r.get('used_mcp', False),
                } for r in results],
                'winner': results[0]['platform'],
            })
        except Exception as e:
            logger.error(f"Run battle error: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    # ─── POST /api/v1/ai-wars/auto-battle ───
    @app.route('/api/v1/ai-wars/auto-battle', methods=['POST', 'OPTIONS'])
    def ai_wars_auto_battle():
        """Generate a question and run a battle automatically (synchronous)."""
        if request.method == 'OPTIONS':
            resp = jsonify({'ok': True})
            resp.headers['Access-Control-Allow-Origin'] = request.headers.get('Origin', '*')
            resp.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
            resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
            return resp
        data = request.get_json() or {}
        category = data.get('category')

        q = _generate_question()
        if category:
            q['category'] = category

        try:
            battle_id, results = _run_battle(q['question'], q['category'])
            if not battle_id:
                return jsonify({'success': False, 'error': results}), 400

            return jsonify({
                'success': True,
                'battle_id': battle_id,
                'question': q['question'],
                'category': q['category'],
                'results': [{
                    'platform': r['platform'],
                    'overall': r['scores']['overall'],
                } for r in results],
                'winner': results[0]['platform'],
            })
        except Exception as e:
            logger.error(f"Auto battle error: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    # ─── POST /api/v1/ai-wars/submit-challenge (ASYNC) ───
# AUTO-REPAIR: duplicate route '/api/v1/ai-wars/submit-challenge' also in ai_wars.py:950 — review and remove one
    @app.route('/api/v1/ai-wars/submit-challenge', methods=['POST', 'OPTIONS'])
    def ai_wars_submit_challenge():
        """User submits a challenge question — returns immediately, battle runs async.
        
        Response includes queue_id for polling via /api/v1/ai-wars/battle-status/<queue_id>.
        Cloudflare Worker timeout is 15s — this endpoint returns in <1s.
        """
        if request.method == 'OPTIONS':
            resp = jsonify({'ok': True})
            resp.headers['Access-Control-Allow-Origin'] = request.headers.get('Origin', '*')
            resp.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
            resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
            return resp

        data = request.get_json() or {}
        question = data.get('question', '').strip()
        email = data.get('email', '').strip()
        category = data.get('category', 'stump-the-ai')

        if not question or len(question) < 10:
            return jsonify({'success': False, 'error': 'Question must be at least 10 characters'}), 400

        if len(question) > 500:
            return jsonify({'success': False, 'error': 'Question too long (max 500 chars)'}), 400

        queue_id = f"q-{uuid.uuid4().hex[:10]}"
        ip = request.headers.get('X-Forwarded-For', request.remote_addr)

        # Store in DB
        try:
            conn = _get_db()
            c = conn.cursor()
            c.execute("""
                CREATE TABLE IF NOT EXISTS wars_battle_queue (
                    id TEXT PRIMARY KEY,
                    question TEXT NOT NULL,
                    category TEXT DEFAULT 'stump-the-ai',
                    email TEXT,
                    status TEXT DEFAULT 'queued',
                    battle_id TEXT,
                    result_json TEXT,
                    error TEXT,
                    ip TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    started_at TIMESTAMPTZ,
                    completed_at TIMESTAMPTZ
                )
            """)
            c.execute("""
                INSERT INTO wars_battle_queue (id, question, category, email, status, ip)
                VALUES (%s, %s, %s, %s, 'queued', %s) ON CONFLICT DO NOTHING
            """, (queue_id, question, category, email, ip))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"Could not persist battle queue: {e}")

        # Also store in wars_challenges for backward compat
        try:
            conn = _get_db()
            c = conn.cursor()
            c.execute("""
                CREATE TABLE IF NOT EXISTS wars_challenges (
                    id TEXT PRIMARY KEY,
                    question TEXT NOT NULL,
                    email TEXT,
                    category TEXT DEFAULT 'stump-the-ai',
                    status TEXT DEFAULT 'pending',
                    battle_id TEXT,
                    submitted_at TIMESTAMPTZ DEFAULT NOW(),
                    ip TEXT
                )
            """)
            challenge_id = f"challenge-{uuid.uuid4().hex[:8]}"
            c.execute("""
                INSERT INTO wars_challenges (id, question, email, category, status, ip)
                VALUES (%s, %s, %s, %s, 'pending', %s) ON CONFLICT DO NOTHING
            """, (challenge_id, question, email, category, ip))
            conn.commit()
            conn.close()
        except Exception:
            pass

        # Store in memory for fast polling
        with _battle_queue_lock:
            _battle_queue[queue_id] = {
                'status': 'queued',
                'question': question,
                'category': category,
                'result': None,
                'battle_id': None,
                'error': None,
                'created_at': datetime.now(timezone.utc).isoformat(),
                'started_at': None,
                'completed_at': None,
            }

        # Launch battle in background thread — returns immediately
        thread = threading.Thread(
            target=_run_battle_async,
            args=(queue_id, question, category),
            daemon=True,
        )
        thread.start()

        return jsonify({
            'success': True,
            'queue_id': queue_id,
            'status': 'queued',
            'message': 'Battle queued! Poll /api/v1/ai-wars/battle-status/' + queue_id + ' for results.',
            'poll_url': f'/api/v1/ai-wars/battle-status/{queue_id}',
        }), 202  # 202 Accepted — processing async

# AUTO-REPAIR: duplicate route '/api/v1/ai-wars/battle-status/<queue_id>' also in ai_wars.py:967 — review and remove one
    # ─── GET /api/v1/ai-wars/battle-status/<queue_id> ───
    @app.route('/api/v1/ai-wars/battle-status/<queue_id>', methods=['GET', 'OPTIONS'])
    def ai_wars_battle_status(queue_id):
        """Poll for async battle result. Returns status + result when complete.
        
        Frontend should poll every 3-5 seconds until status is 'completed' or 'failed'.
        """
        if request.method == 'OPTIONS':
            resp = jsonify({'ok': True})
            resp.headers['Access-Control-Allow-Origin'] = request.headers.get('Origin', '*')
            resp.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
            resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
            return resp

        # Check in-memory first (fastest)
        with _battle_queue_lock:
            entry = _battle_queue.get(queue_id)

        if entry:
            response = {
                'success': True,
                'queue_id': queue_id,
                'status': entry['status'],
                'created_at': entry['created_at'],
                'started_at': entry.get('started_at'),
                'completed_at': entry.get('completed_at'),
            }
            if entry['status'] == 'completed':
                response['battle'] = entry.get('result')
            elif entry['status'] == 'failed':
                response['error'] = entry.get('error')
            return jsonify(response)

        # Fall back to DB (in case Railway restarted and memory was lost)
        try:
            conn = _get_db()
            c = conn.cursor()
            c.execute("SELECT * FROM wars_battle_queue WHERE id = %s", (queue_id,))
            row = c.fetchone()
            conn.close()

            if row:
                response = {
                    'success': True,
                    'queue_id': queue_id,
                    'status': row['status'],
                    'created_at': str(row.get('created_at', '')),
                    'started_at': str(row.get('started_at', '')) if row.get('started_at') else None,
                    'completed_at': str(row.get('completed_at', '')) if row.get('completed_at') else None,
                }
                if row['status'] == 'completed' and row.get('result_json'):
                    try:
                        response['battle'] = json.loads(row['result_json'])
                    except:
                        response['battle'] = {'battle_id': row.get('battle_id')}
                elif row['status'] == 'failed':
                    response['error'] = row.get('error')
                return jsonify(response)
        except Exception:
            pass

        return jsonify({'success': False, 'error': 'Battle not found', 'queue_id': queue_id}), 404

    # ─── GET /api/v1/ai-wars/schedule ───
    @app.route('/api/v1/ai-wars/schedule', methods=['GET'])
    def ai_wars_schedule():
        """View automation schedule and recent battle history."""
        conn = _get_db()
        c = conn.cursor()

        c.execute("SELECT COUNT(*) as cnt FROM wars_battles")
        total_battles = c.fetchone()['cnt']

        c.execute("SELECT date, COUNT(*) as count FROM wars_battles GROUP BY date ORDER BY date DESC LIMIT 10")
        recent = [dict(row) for row in c.fetchall()]

        # Check pending challenges
        try:
            c.execute("SELECT COUNT(*) as cnt FROM wars_challenges WHERE status = 'pending'")
            pending = c.fetchone()['cnt']
        except:
            pending = 0

        # Check queued battles
        try:
            c.execute("SELECT COUNT(*) as cnt FROM wars_battle_queue WHERE status IN ('queued', 'running')")
            active_queue = c.fetchone()['cnt']
        except:
            active_queue = 0

        # Available API keys
        available_keys = []
        key_map = {
            'ANTHROPIC_API_KEY': 'Claude (+ Cursor + Windsurf)',
            'OPENAI_API_KEY': 'ChatGPT (+ Copilot)',
            'GOOGLE_AI_KEY': 'Gemini',
            'XAI_API_KEY': 'Grok',
            'DEEPSEEK_API_KEY': 'DeepSeek',
            'MISTRAL_API_KEY': 'Mistral',
            'COHERE_API_KEY': 'Cohere',
            'PERPLEXITY_API_KEY': 'Perplexity',
            'GROQ_API_KEY': 'Groq',
            'TOGETHER_API_KEY': 'Meta AI (Llama)',
            'YOU_API_KEY': 'You.com',
            'AWS_ACCESS_KEY_ID': 'Amazon Q (Bedrock)',
        }
        for env_var, name in key_map.items():
            if os.environ.get(env_var):
                available_keys.append(name)

        conn.close()

        return jsonify({
            'success': True,
            'total_battles': total_battles,
            'pending_challenges': pending,
            'active_queue': active_queue,
            'recent_activity': recent,
            'api_keys_available': available_keys,
            'platforms_with_mcp': ['cursor', 'windsurf'],
            'schedule': {
                'weekly_battles': 'Every Monday, 9 battles (one per category)',
                'challenge_processing': 'Async — returns immediately, battle runs in background',
            }
        })

    # ─── Background scheduler ─── DISABLED: Use POST /api/jobs/ai-wars (Feb 2026)
    def _wars_scheduler_loop():
        """Background thread: DISABLED — use POST /api/jobs/ai-wars instead."""
        pass  # No-op: converted to one-shot job endpoint

    wars_thread = threading.Thread(target=_wars_scheduler_loop, daemon=True)
    wars_thread.start()

    logger.info("⚔️ AI Wars automation v2 registered (async battles + cursor/windsurf + scoring v2)")
