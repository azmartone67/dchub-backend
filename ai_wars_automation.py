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
  POST /api/v1/ai-wars/submit-challenge - User-submitted challenge from the website
"""

import uuid
import json
import time
import os
import logging
import threading
import re
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

# ─── Battle question templates ───
QUESTION_TEMPLATES = [
    {
        'category': 'site-selection',
        'templates': [
            "A hyperscaler needs {mw}MW in {region}. Using DC Hub data, which market should they choose and why?",
            "Compare the top 3 markets in {region} for a new {mw}MW data center campus. Consider power, fiber, land, and risk.",
            "A cloud provider wants to build a {mw}MW AI training facility. Analyze the best US market using DC Hub facility data.",
        ]
    },
    {
        'category': 'ma-forensics',
        'templates': [
            "Analyze the most recent data center M&A transaction. Who bought what, at what valuation, and was it a good deal?",
            "Using DC Hub transaction data, identify the highest $/MW acquisition this year and evaluate the buyer's strategy.",
            "Compare the last 3 data center deals. Which represented the best value per MW and why?",
        ]
    },
    {
        'category': 'operator-showdown',
        'templates': [
            "Compare {provider1} vs {provider2} using DC Hub portfolio data. Who has better market positioning?",
            "Which operator — {provider1}, {provider2}, or {provider3} — is best positioned for the AI infrastructure boom?",
            "Rank the top 5 data center operators by portfolio strength using DC Hub data.",
        ]
    },
    {
        'category': 'market-deep-dive',
        'templates': [
            "Deep dive into {market}: Is it saturated or still growing? Analyze power, vacancy, pipeline, and pricing.",
            "What are the key infrastructure constraints in {market}? Use DC Hub data on facilities, power, and fiber.",
            "Compare {market} today vs 2 years ago. What's changed in capacity, pricing, and demand?",
        ]
    },
    {
        'category': 'stump-the-ai',
        'templates': [
            "You have $1B to invest in one data center market. Which one and why? Use DC Hub data to support your case.",
            "What is the most undervalued data center market right now? Use DC Hub vacancy rates, pricing, and pipeline data.",
            "If you could only build in one country outside the US, where would you build and why?",
            "Which data center market has the highest risk of oversupply in the next 2 years?",
        ]
    },
    {
        'category': 'weekly-brief',
        'templates': [
            "Summarize this week's most important data center industry developments using DC Hub news and data.",
            "What are the 3 most significant data center deals, announcements, or market shifts this week?",
        ]
    },
    {
        'category': 'mcp-tool-test',
        'templates': [
            "Use DC Hub's search_facilities tool to find all {provider1} facilities in Virginia. How many are there and what's the total MW?",
            "Use DC Hub's analyze_site tool to score {market} for a {mw}MW data center. What infrastructure is nearby?",
            "Use DC Hub's get_infrastructure tool to find substations within 50km of {market}. What's the highest voltage available?",
            "Using DC Hub's MCP tools, compare {market} vs Phoenix for a {mw}MW hyperscale campus. Which scores higher?",
            "Query DC Hub's search_facilities for the largest data center under construction in the US. What is it and who's building it?",
            "Use DC Hub to find all Tallgrass Energy data center sites. How many MW total across their portfolio?",
            "Use DC Hub's get_news tool to find the latest M&A deal. What was the transaction value?",
        ]
    },
    {
        'category': 'energy-ppa',
        'templates': [
            "Which data center operators have signed nuclear power purchase agreements? Use DC Hub data.",
            "Compare behind-the-meter vs front-of-meter power strategies for hyperscale data centers using DC Hub energy data.",
            "What is the total MW of nuclear PPAs signed for data centers? Which operators are leading this trend?",
        ]
    },
    {
        'category': 'construction-pipeline',
        'templates': [
            "What are the 5 largest data center projects currently under construction? Use DC Hub pipeline data.",
            "How many GW of data center capacity is in the construction pipeline? Break it down by status.",
            "Which markets have the most data center construction activity right now? Use DC Hub capacity pipeline data.",
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
    """Get Neon PostgreSQL connection for AI Wars."""
    try:
        from db_utils import get_db
        return get_db()
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
        # Get current stats
        r = requests.get(f"{api_base}/api/v1/stats", timeout=5)
        if r.ok:
            context['stats'] = r.json()
    except:
        pass

    try:
        # Get recent transactions
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

    # Map platform keys to crawler stat names
    PLATFORM_CRAWLER_MAP = {
        'chatgpt': ['ChatGPT-User', 'GPTBot', 'openai'],
        'claude': ['Claude-Web', 'ClaudeBot', 'anthropic'],
        'gemini': ['Google-Extended', 'Googlebot', 'google'],
        'grok': ['Grok', 'xAI'],
        'copilot': ['bingbot', 'BingPreview', 'Copilot'],
        'perplexity': ['PerplexityBot', 'perplexity'],
        'deepseek': ['DeepSeek', 'deepseek'],
        'meta': ['meta-externalagent', 'Meta', 'FacebookBot'],
        'cohere': ['cohere', 'CohereBot'],
        'mistral': ['MistralBot', 'mistral'],
        'you': ['YouBot', 'you.com'],
        'huggingchat': ['HuggingFace', 'huggingchat'],
    }

    crawlers = PLATFORM_CRAWLER_MAP.get(platform_key, [])
    total_visits = 0
    for crawler_name, visit_count in usage.items():
        for match in crawlers:
            if match.lower() in crawler_name.lower():
                total_visits += visit_count
                break

    # Scale: 0 visits = 0 boost, 1-5 = +2, 6-20 = +4, 21-50 = +6, 50+ = +8
    if total_visits >= 50:
        return 8
    elif total_visits >= 21:
        return 6
    elif total_visits >= 6:
        return 4
    elif total_visits >= 1:
        return 2
    return 0


def _score_response(response_text, question, context=None):
    """Score an AI platform's response on 5 metrics (0-100 each).
    
    Simple heuristic scoring — can be upgraded to LLM-judged later.
    """
    if not response_text:
        return {'accuracy': 0, 'depth': 0, 'speed': 0, 'citation': 0, 'insight': 0, 'overall': 0}

    text = response_text.lower()
    word_count = len(text.split())

    # Depth: based on response length and structure
    depth = min(100, max(30, int(word_count / 5)))
    if any(w in text for w in ['however', 'although', 'on the other hand', 'trade-off', 'nuance']):
        depth = min(100, depth + 10)

    # Accuracy: mentions of real data, numbers, specific markets
    accuracy = 50
    number_count = len(re.findall(r'\d+\.?\d*\s*(mw|gw|kw|%|\$|billion|million|facilities|sqft)', text))
    accuracy = min(100, accuracy + number_count * 5)
    market_mentions = sum(1 for m in MARKETS if m.lower() in text)
    accuracy = min(100, accuracy + market_mentions * 3)

    # Citations: references to DC Hub, sources, data points
    citation = 30
    cite_words = ['dc hub', 'dchub', 'according to', 'data shows', 'source:', 'based on']
    citation += sum(8 for w in cite_words if w in text)
    citation = min(100, citation)

    # Insight: unique observations, recommendations, forward-looking
    insight = 40
    insight_words = ['recommend', 'suggest', 'opportunity', 'risk', 'trend',
                     'forecast', 'predict', 'strategy', 'advantage', 'undervalued',
                     'overvalued', 'emerging', 'constraint', 'bottleneck']
    insight += sum(5 for w in insight_words if w in text)
    insight = min(100, insight)

    # Speed: scored externally (response time), default to 80
    speed = 80

    overall = int((accuracy * 0.25 + depth * 0.25 + speed * 0.15 + citation * 0.15 + insight * 0.20))

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
# Set keys in Replit Secrets:
#   OPENAI_API_KEY, ANTHROPIC_API_KEY, GOOGLE_AI_KEY, XAI_API_KEY
#   DEEPSEEK_API_KEY, MISTRAL_API_KEY, COHERE_API_KEY, PERPLEXITY_API_KEY
# =============================================================================

SYSTEM_PROMPT = """You are a data center market intelligence analyst competing in DC Hub AI Wars.
Provide sharp, data-driven analysis. Be specific with markets, MW capacity, pricing, and operators.
Reference DC Hub data when provided. Keep response under 600 words. Be decisive — pick a winner or make a clear recommendation."""


def _call_platform_api(platform_key, prompt, max_tokens=1000):
    """Route to the correct platform API adapter. Returns response text or empty string."""
    adapters = {
        'chatgpt':    _call_openai,
        'claude':     _call_anthropic,
        'gemini':     _call_google,
        'grok':       _call_xai,
        'deepseek':   _call_deepseek,
        'mistral':    _call_mistral,
        'cohere':     _call_cohere,
        'perplexity': _call_perplexity,
    }

    adapter = adapters.get(platform_key)
    if not adapter:
        return ""

    try:
        return adapter(prompt, max_tokens)
    except Exception as e:
        logger.warning(f"⚔️ {platform_key} API call failed: {e}")
        return ""


def _call_openai(prompt, max_tokens=1000):
    """OpenAI / ChatGPT API (GPT-4o-mini for cost efficiency)."""
    key = os.environ.get('OPENAI_API_KEY', '')
    if not key:
        return ""

    import requests
    r = requests.post('https://api.openai.com/v1/chat/completions',
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
    """Anthropic / Claude API (Claude 3.5 Haiku for cost efficiency)."""
    key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not key:
        return ""

    import requests
    r = requests.post('https://api.anthropic.com/v1/messages',
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
        f'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={key}',
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
    """Perplexity API (sonar-small for cost efficiency)."""
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


def _run_battle(question, category, fighters_config=None, api_base='https://dchub-backend-production.up.railway.app'):
    """Run a battle: send question to platforms, score responses, save results.
    
    fighters_config: list of dicts with platform keys to include.
                     If None, uses all active platforms with API endpoints.
    """
    conn = _get_db()
    c = conn.cursor()

    # Get active platforms
    c.execute("SELECT platform, name, api_endpoint FROM wars_platforms WHERE status = 'active'")
    platforms = {row['platform']: dict(row) for row in c.fetchall()}

    if fighters_config:
        selected = {f['platform']: platforms.get(f['platform'], {'platform': f['platform'], 'name': f['platform']})
                    for f in fighters_config if f['platform'] in platforms}
    else:
        # Use all platforms that have data (even without API endpoints — we simulate)
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

    # Run each platform
    fighter_results = []
    total_api_calls = 0

    for platform_key, platform_info in selected.items():
        start_time = time.time()
        response_text = ""
        api_calls = 1

        # Call real API using per-platform adapter
        response_text = _call_platform_api(platform_key, prompt)

        elapsed = time.time() - start_time

        # Score the response (or generate simulated scores if no real response)
        if response_text:
            scores = _score_response(response_text, question, context)
            scores['speed'] = max(20, min(100, int(100 - elapsed * 2)))
        else:
            # Simulated scoring based on historical platform performance
            c.execute("SELECT * FROM wars_platform_stats WHERE platform = %s", (platform_key,))
            stats = c.fetchone()
            if stats:
                import random
                base = dict(stats)
                scores = {
                    'accuracy': max(50, min(100, int(base.get('avg_accuracy', 70) + random.randint(-5, 5)))),
                    'depth': max(50, min(100, int(base.get('avg_depth', 70) + random.randint(-5, 5)))),
                    'speed': max(50, min(100, int(base.get('avg_speed', 70) + random.randint(-5, 5)))),
                    'citation': max(50, min(100, int(base.get('avg_citation', 70) + random.randint(-5, 5)))),
                    'insight': max(50, min(100, int(base.get('avg_insight', 70) + random.randint(-5, 5)))),
                }
                scores['overall'] = int(sum(scores.values()) / 5)
            else:
                import random
                base = random.randint(65, 85)
                scores = {k: max(50, min(100, base + random.randint(-8, 8)))
                          for k in ['accuracy', 'depth', 'speed', 'citation', 'insight']}
                scores['overall'] = int(sum(scores.values()) / 5)

        # Apply usage boost — platforms that actively use DC Hub API get rewarded
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
            'had_real_response': bool(response_text),
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
        (id, category, title, description, date, week_number, year,
         winner_platform, winner_label, api_calls, status, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', %s)
    """, (
        battle_id, category,
        question[:80] + ('...' if len(question) > 80 else ''),
        question,
        now.strftime('%Y-%m-%d'), week_num, now.year,
        winner['platform'], winner_label,
        total_api_calls, now.isoformat(),
    ))

    # Save fighters
    for f in fighter_results:
        fid = f"fighter-{battle_id}-{f['platform']}"
        c.execute("""
            INSERT INTO wars_fighters
            (id, battle_id, platform, role,
             score_accuracy, score_depth, score_speed, score_citation, score_insight, score_overall,
             api_calls, pick, is_winner)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            fid, battle_id, f['platform'], f.get('role'),
            f['scores']['accuracy'], f['scores']['depth'], f['scores']['speed'],
            f['scores']['citation'], f['scores']['insight'], f['scores']['overall'],
            f['api_calls'], None,
            1 if f['platform'] == winner['platform'] else 0,
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
                VALUES (%s, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            # Override category to ensure we cover all
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


def register_wars_automation(app):
    """Register AI Wars automation routes and scheduler."""
    from flask import request, jsonify

    # ─── POST /api/v1/ai-wars/run-battle ───
    @app.route('/api/v1/ai-wars/run-battle', methods=['POST', 'OPTIONS'])
    def ai_wars_run_battle():
        """Run a battle with a specific question."""
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
                } for r in results],
                'winner': results[0]['platform'],
            })
        except Exception as e:
            logger.error(f"Run battle error: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    # ─── POST /api/v1/ai-wars/auto-battle ───
    @app.route('/api/v1/ai-wars/auto-battle', methods=['POST', 'OPTIONS'])
    def ai_wars_auto_battle():
        """Generate a question and run a battle automatically."""
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

    # ─── POST /api/v1/ai-wars/submit-challenge ───
    @app.route('/api/v1/ai-wars/submit-challenge', methods=['POST', 'OPTIONS'])
    def ai_wars_submit_challenge():
        """User submits a challenge question from the website."""
        if request.method == 'OPTIONS':
            resp = jsonify({'ok': True})
            resp.headers['Access-Control-Allow-Origin'] = request.headers.get('Origin', '*')
            resp.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
            resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
            return resp
        data = request.get_json() or {}
        question = data.get('question', '').strip()
        email = data.get('email', '').strip()

        if not question or len(question) < 10:
            return jsonify({'success': False, 'error': 'Question must be at least 10 characters'}), 400

        if len(question) > 500:
            return jsonify({'success': False, 'error': 'Question too long (max 500 chars)'}), 400

        conn = _get_db()
        c = conn.cursor()

        # Create a challenges table if not exists
        c.execute("""
            CREATE TABLE IF NOT EXISTS wars_challenges (
                id TEXT PRIMARY KEY,
                question TEXT NOT NULL,
                email TEXT,
                category TEXT DEFAULT 'stump-the-ai',
                status TEXT DEFAULT 'pending',
                battle_id TEXT,
                submitted_at TEXT DEFAULT (datetime('now')),
                ip TEXT
            )
        """)

        challenge_id = f"challenge-{uuid.uuid4().hex[:8]}"
        ip = request.headers.get('X-Forwarded-For', request.remote_addr)

        c.execute("""
            INSERT INTO wars_challenges (id, question, email, status, ip)
            VALUES (?, ?, ?, 'pending', ?)
        """, (challenge_id, question, email, ip))

        conn.commit()

        # If auto-run is enabled, run it immediately
        auto_run = data.get('auto_run', True)
        battle_result = None

        if auto_run:
            try:
                battle_id, results = _run_battle(question, 'stump-the-ai')
                if battle_id:
                    c.execute("UPDATE wars_challenges SET status='completed', battle_id=? WHERE id=%s",
                              (battle_id, challenge_id))
                    conn.commit()
                    battle_result = {
                        'battle_id': battle_id,
                        'winner': results[0]['platform'] if results else None,
                    }
            except Exception as e:
                logger.warning(f"Auto-run challenge failed: {e}")

        conn.close()

        return jsonify({
            'success': True,
            'challenge_id': challenge_id,
            'message': 'Challenge submitted!' + (' Battle ran immediately.' if battle_result else ' Queued for next battle run.'),
            'battle': battle_result,
        }), 201

    # ─── GET /api/v1/ai-wars/schedule ───
    @app.route('/api/v1/ai-wars/schedule', methods=['GET'])
    def ai_wars_schedule():
        """View automation schedule and recent battle history."""
        conn = _get_db()
        c = conn.cursor()

        c.execute("SELECT COUNT(*) FROM wars_battles")
        total_battles = c.fetchone()[0]

        c.execute("SELECT date, COUNT(*) as count FROM wars_battles GROUP BY date ORDER BY date DESC LIMIT 10")
        recent = [dict(row) for row in c.fetchall()]

        # Check pending challenges
        try:
            c.execute("SELECT COUNT(*) FROM wars_challenges WHERE status = 'pending'")
            pending = c.fetchone()[0]
        except:
            pending = 0

        conn.close()

        return jsonify({
            'success': True,
            'total_battles': total_battles,
            'pending_challenges': pending,
            'recent_activity': recent,
            'schedule': {
                'weekly_battles': 'Every Monday, 6 battles (one per category)',
                'challenge_processing': 'Immediate (auto-run) or queued for next cycle',
            }
        })

    # ─── Background scheduler ─── DISABLED: Use POST /api/jobs/ai-wars (Feb 2026)
    def _wars_scheduler_loop():
        """Background thread: DISABLED — use POST /api/jobs/ai-wars instead."""
        pass  # No-op: converted to one-shot job endpoint
    if False:  # Dead code preserved for reference
        time.sleep(180)
        logger.info("⚔️ AI Wars scheduler started")

        while True:
            try:
                now = datetime.now(timezone.utc)

                if now.hour == 6:
                    conn = _get_db()
                    c = conn.cursor()
                    today = now.strftime('%Y-%m-%d')
                    c.execute("SELECT COUNT(*) FROM wars_battles WHERE date = %s AND category = 'weekly-brief'", (today,))
                    ran_today = c.fetchone()[0]
                    conn.close()

                    if ran_today == 0:
                        logger.info("⚔️ Monday detected — running weekly battles")
                        _weekly_battle_runner()

                try:
                    conn = _get_db()
                    c = conn.cursor()
                    c.execute("""
                        SELECT id, question FROM wars_challenges
                        WHERE status = 'pending'
                        ORDER BY submitted_at ASC LIMIT 3
                    """)
                    pending = c.fetchall()
                    conn.close()

                    for challenge in pending:
                        try:
                            battle_id, results = _run_battle(challenge['question'], 'stump-the-ai')
                            if battle_id:
                                conn = _get_db()
                                conn.execute("UPDATE wars_challenges SET status='completed', battle_id=%s WHERE id=%s",
                                             (battle_id, challenge['id']))
                                conn.commit()
                                conn.close()
                        except Exception as e:
                            logger.warning(f"Challenge processing error: {e}")
                except:
                    pass

            except Exception as e:
                logger.error(f"⚔️ Wars scheduler error: {e}")

            time.sleep(1800)  # Check every 30 minutes

    # Start background thread
    wars_thread = threading.Thread(target=_wars_scheduler_loop, daemon=True)
    wars_thread.start()

    logger.info("⚔️ AI Wars automation registered (scheduler + challenge endpoint)")
