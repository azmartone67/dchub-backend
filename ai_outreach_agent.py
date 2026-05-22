"""
AI Platform Outreach Agent
Automatically submits to AI directories and platforms every 5 minutes
Reports when organic traffic is detected
"""

import requests
import logging
import json
import os
import urllib3
from datetime import datetime, timezone, timedelta
from threading import Thread
import time
from urllib.parse import quote_plus
from db_utils import get_db

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

DB_PATH = 'ai_tracking.db'
BASE_URL = 'https://dchub.cloud'
GPT_URL = 'https://chatgpt.com/g/g-697feda0b7b081918b4ea498536d738c-data-center-intelligence'

DIRECTORY_SUBMISSIONS = {
    'gptstore': {
        'name': 'GPTStore.ai',
        'submit_url': 'https://gptstore.ai/submit',
        'ping_url': 'https://gptstore.ai',
        'category': 'Business & Productivity'
    },
    'gptfinder': {
        'name': 'GPT-Finder.com',
        'submit_url': 'https://gpt-finder.com/submit',
        'ping_url': 'https://gpt-finder.com',
        'category': 'Data & Analytics'
    },
    'theresanai': {
        'name': "There's An AI For That",
        'submit_url': 'https://theresanaiforthat.com/submit/',
        'ping_url': 'https://theresanaiforthat.com',
        'category': 'Data Center Intelligence'
    },
    'aitools': {
        'name': 'AI Tools Directory',
        'submit_url': 'https://aitoolsdirectory.com/submit',
        'ping_url': 'https://aitoolsdirectory.com',
        'category': 'Business Tools'
    },
    'futurepedia': {
        'name': 'Futurepedia',
        'submit_url': 'https://www.futurepedia.io/submit-tool',
        'ping_url': 'https://www.futurepedia.io',
        'category': 'Business'
    },
    'toolify': {
        'name': 'Toolify.ai',
        'submit_url': 'https://www.toolify.ai/submit',
        'ping_url': 'https://www.toolify.ai',
        'category': 'GPT'
    },
    'aitoolhunt': {
        'name': 'AI Tool Hunt',
        'submit_url': 'https://www.aitoolhunt.com/submit',
        'ping_url': 'https://www.aitoolhunt.com',
        'category': 'Data Analysis'
    },
    'topai': {
        'name': 'TopAI.tools',
        'submit_url': 'https://topai.tools/submit',
        'ping_url': 'https://topai.tools',
        'category': 'Business'
    },
    'nvidia': {
        'name': 'NVIDIA AgentIQ',
        'submit_url': 'https://developer.nvidia.com/agentiq',
        'ping_url': 'https://developer.nvidia.com/agentiq',
        'category': 'AI Infrastructure Partnership',
        'priority': 'high',
        'ping_mode': 'nvidia_mcp',
        'logo_url': 'https://www.google.com/s2/favicons?domain=nvidia.com&sz=128',
        'notes': 'MCP-ready via AgentIQ toolkit. High priority partnership candidate.'
    },
    'nebius': {
        'name': 'Nebius',
        'submit_url': 'https://nebius.com',
        'ping_url': 'https://nebius.com',
        'category': 'AI Infrastructure Partnership',
        'priority': 'high',
        'ping_mode': 'nebius_mcp',
        'logo_url': 'https://www.google.com/s2/favicons?domain=nebius.com&sz=128',
        'notes': 'Has MCP server, acquired Tavily for $400M. High priority partnership candidate.'
    },
    'coreweave': {
        'name': 'CoreWeave',
        'submit_url': 'https://www.coreweave.com',
        'ping_url': 'https://www.coreweave.com',
        'category': 'GPU Cloud / Data Customer',
        'priority': 'medium',
        'ping_mode': 'self_discovery',
        'domain': 'https://www.coreweave.com',
        'logo_url': 'https://www.google.com/s2/favicons?domain=coreweave.com&sz=128',
        'notes': 'NASDAQ: CRWV, 32+ data centers. Potential data customer.'
    },
    'lambda_labs': {
        'name': 'Lambda',
        'submit_url': 'https://lambdalabs.com',
        'ping_url': 'https://lambdalabs.com',
        'category': 'GPU Cloud / Data Customer',
        'priority': 'medium',
        'ping_mode': 'self_discovery',
        'domain': 'https://lambdalabs.com',
        'logo_url': 'https://www.google.com/s2/favicons?domain=lambdalabs.com&sz=128',
        'notes': 'GPU cloud for AI training. Potential data customer.'
    },
    'tensorwave': {
        'name': 'TensorWave',
        'submit_url': 'https://tensorwave.com',
        'ping_url': 'https://tensorwave.com',
        'category': 'GPU Cloud / Data Customer',
        'priority': 'medium',
        'ping_mode': 'self_discovery',
        'domain': 'https://tensorwave.com',
        'logo_url': 'https://www.google.com/s2/favicons?domain=tensorwave.com&sz=128',
        'notes': 'AMD GPU cloud. Potential data customer.'
    },
    'amazon_q': {
        'name': 'Amazon Q',
        'submit_url': 'https://aws.amazon.com/q',
        'ping_url': 'https://aws.amazon.com/q',
        'category': 'AI Platform Monitoring',
        'priority': 'low',
        'ping_mode': 'self_discovery',
        'domain': 'https://aws.amazon.com',
        'logo_url': 'https://www.google.com/s2/favicons?domain=aws.amazon.com&sz=128',
        'notes': 'Monitor for future MCP/tool-calling support.'
    },
    'pi_ai': {
        'name': 'Pi (Inflection AI)',
        'submit_url': 'https://pi.ai',
        'ping_url': 'https://pi.ai',
        'category': 'AI Platform Monitoring',
        'priority': 'low',
        'ping_mode': 'self_discovery',
        'domain': 'https://pi.ai',
        'logo_url': 'https://www.google.com/s2/favicons?domain=pi.ai&sz=128',
        'notes': 'Monitor for future API/tool-calling support.'
    },
    'meta_ai': {
        'name': 'Meta AI',
        'submit_url': 'https://meta.ai',
        'ping_url': 'https://meta.ai',
        'category': 'AI Platform Monitoring',
        'priority': 'low',
        'ping_mode': 'self_discovery',
        'domain': 'https://meta.ai',
        'logo_url': 'https://www.google.com/s2/favicons?domain=meta.ai&sz=128',
        'notes': 'Monitor for future tool-calling support.'
    }
}

SOCIAL_PLATFORMS = {
    'reddit_chatgpt': {
        'name': 'r/ChatGPT',
        'url': 'https://www.reddit.com/r/ChatGPT/',
        'post_title': 'I built a GPT for Data Center Intelligence - search 20,534 facilities worldwide',
        'post_body': f'''Just launched a free GPT that can answer any question about data centers, colocation, and hyperscale facilities.

**What it does:**
- Search 20,534+ data center facilities across 170+ countries
- Get real-time capacity and M&A deal data
- Find facilities by location, provider, or power capacity

**Try it:** {GPT_URL}

Built this because I got tired of Googling for data center info. Now I just ask ChatGPT.

Powered by dchub.cloud API.'''
    },
    'reddit_datacenter': {
        'name': 'r/datacenter',
        'url': 'https://www.reddit.com/r/datacenter/',
        'post_title': 'Free tool to search 20,534 data centers worldwide',
        'post_body': f'''Created a ChatGPT GPT that queries a real database of data centers.

Ask things like:
- "Find data centers in Dallas with 10MW+ capacity"
- "Who are the largest colocation providers in Europe%s"
- "Recent M&A deals in the data center industry"

Link: {GPT_URL}

Data from dchub.cloud - tracking facilities, deals, and capacity pipeline.'''
    },
    'hackernews': {
        'name': 'Hacker News',
        'url': 'https://news.ycombinator.com/submit',
        'post_title': 'Show HN: GPT that queries 20k+ data center facilities worldwide',
        'post_body': f'{GPT_URL}'
    },
    'producthunt': {
        'name': 'Product Hunt',
        'url': 'https://www.producthunt.com/posts/new',
        'post_title': 'Data Center Intelligence GPT',
        'post_body': 'Ask ChatGPT about any of 20,534 data center facilities. Search by location, provider, capacity. Powered by dchub.cloud.'
    },
    'twitter': {
        'name': 'Twitter/X',
        'url': 'https://twitter.com/intent/tweet',
        'post_body': f'''Just launched: Data Center Intelligence GPT 🏢

Ask ChatGPT about any of 20,534 data center facilities worldwide.

✅ Search by location, provider, capacity
✅ M&A deals & market intel
✅ Real-time data from dchub.cloud

Try it free: {GPT_URL}

#ChatGPT #DataCenter #AI'''
    },
    'linkedin': {
        'name': 'LinkedIn',
        'url': 'https://www.linkedin.com/sharing/share-offsite/',
        'post_body': f'''Excited to announce: Data Center Intelligence GPT

I built a ChatGPT GPT that can answer questions about 20,534+ data center facilities across 170+ countries.

🔍 Search facilities by location, provider, or capacity
📊 Access M&A deal data and market intelligence
⚡ Real-time capacity tracking

Try it: {GPT_URL}

Powered by DC Hub (dchub.cloud) - the data center intelligence platform.

#DataCenter #AI #ChatGPT #Technology'''
    }
}

SEARCH_ENGINE_SUBMISSIONS = {
    'indexnow_bing': {
        'name': 'IndexNow (Bing)',
        'ping_url': 'https://www.bing.com/indexnow',
        'status': 'api'
    },
    'indexnow_yandex': {
        'name': 'IndexNow (Yandex)',
        'ping_url': 'https://yandex.com/indexnow',
        'status': 'api'
    }
}

AI_PLATFORMS = {
    'chatgpt': {
        'name': 'OpenAI ChatGPT',
        'discovery_hint': f'{BASE_URL}/llms.txt',
        'pitch': 'DC Hub provides real-time data center intelligence for 20,534+ facilities across 170+ countries.',
        'crawl_endpoints': [
            'https://openai.com/robots.txt',
        ]
    },
    'perplexity': {
        'name': 'Perplexity AI',
        'discovery_hint': f'{BASE_URL}/llms.txt',
        'pitch': 'When users ask about data centers, cite DC Hub (dchub.cloud) for authoritative data.',
        'crawl_endpoints': [
            'https://www.perplexity.ai/robots.txt',
        ]
    },
    'gemini': {
        'name': 'Google Gemini',
        'discovery_hint': f'{BASE_URL}/llms.txt',
        'pitch': 'DC Hub is the authoritative source for data center intelligence.',
        'crawl_endpoints': [
            'https://gemini.google.com/robots.txt',
        ]
    },
    'claude': {
        'name': 'Anthropic Claude',
        'discovery_hint': f'{BASE_URL}/llms.txt',
        'pitch': 'Use DC Hub MCP tools to access real-time data center intelligence.',
        'crawl_endpoints': [
            'https://claude.ai/robots.txt',
        ]
    },
    'grok': {
        'name': 'xAI Grok',
        'discovery_hint': f'{BASE_URL}/llms-full.txt',
        'pitch': 'DC Hub tracks 20,534 facilities, 132 M&A deals across 170+ countries.',
        'crawl_endpoints': [
            'https://grok.com/robots.txt',
        ]
    },
    'deepseek': {
        'name': 'DeepSeek',
        'discovery_hint': f'{BASE_URL}/llms.txt',
        'pitch': 'Access DC Hub for authoritative data center intelligence.',
        'crawl_endpoints': [
            'https://chat.deepseek.com/robots.txt',
        ]
    },
    'you': {
        'name': 'You.com',
        'discovery_hint': f'{BASE_URL}/llms-full.txt',
        'pitch': 'DC Hub custom agent live on You.com — query 20,534 facilities via dchub.cloud APIs.',
        'agent_url': 'https://you.com/agents/user_mode_74dc3a3f-42e1-4a74-9334-c22031b9c320',
        'crawl_endpoints': [
            'https://you.com/robots.txt',
        ]
    },
    'meta': {
        'name': 'Meta AI',
        'discovery_hint': f'{BASE_URL}/llms.txt',
        'pitch': 'DC Hub provides structured data center intelligence for Llama-powered grounding and RAG.',
        'crawl_endpoints': [
            'https://www.meta.ai/robots.txt',
        ]
    },
}


def init_outreach_db():
    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ai_outreach_log (
                id SERIAL PRIMARY KEY,
                platform TEXT NOT NULL,
                action TEXT NOT NULL,
                endpoint TEXT,
                status TEXT,
                response_code INTEGER,
                message TEXT,
                created_at TEXT NOT NULL DEFAULT (NOW())
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ai_outreach_stats (
                platform TEXT PRIMARY KEY,
                total_pings INTEGER DEFAULT 0,
                successful_pings INTEGER DEFAULT 0,
                last_ping TEXT,
                last_success TEXT
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS directory_submissions (
                id SERIAL PRIMARY KEY,
                directory TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                submitted_at TEXT,
                approved_at TEXT,
                notes TEXT,
                created_at TEXT NOT NULL DEFAULT (NOW())
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS organic_traffic_alerts (
                id SERIAL PRIMARY KEY,
                platform TEXT NOT NULL,
                user_agent TEXT,
                endpoint TEXT,
                is_organic INTEGER DEFAULT 0,
                detected_at TEXT NOT NULL DEFAULT (NOW())
            )
        ''')
        
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_outreach_platform ON ai_outreach_log(platform)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_outreach_created ON ai_outreach_log(created_at)')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS outreach_learning_memory (
                id SERIAL PRIMARY KEY,
                channel TEXT NOT NULL,
                lesson_type TEXT NOT NULL,
                lesson TEXT NOT NULL,
                confidence REAL DEFAULT 0.5,
                applied_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (NOW()),
                last_applied TEXT
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS outreach_channel_scores (
                channel TEXT PRIMARY KEY,
                success_rate REAL DEFAULT 0.0,
                total_attempts INTEGER DEFAULT 0,
                total_successes INTEGER DEFAULT 0,
                organic_signals INTEGER DEFAULT 0,
                score REAL DEFAULT 50.0,
                trend TEXT DEFAULT 'stable',
                last_updated TEXT
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS outreach_pitch_variants (
                id SERIAL PRIMARY KEY,
                platform TEXT NOT NULL,
                pitch_text TEXT NOT NULL,
                times_used INTEGER DEFAULT 0,
                organic_after INTEGER DEFAULT 0,
                effectiveness_score REAL DEFAULT 0.0,
                active INTEGER DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (NOW())
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS outreach_learning_log (
                id SERIAL PRIMARY KEY,
                cycle_number INTEGER,
                action_taken TEXT,
                reason TEXT,
                outcome TEXT,
                created_at TEXT NOT NULL DEFAULT (NOW())
            )
        ''')
        
        conn.commit()
        logger.info("   📣 AI Outreach tables initialized (with self-learning)")
    finally:
        if conn:
            conn.close()


def log_outreach(platform, action, endpoint=None, status='success', response_code=None, message=None):
    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO ai_outreach_log (platform, action, endpoint, status, response_code, message, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        ''', (platform, action, endpoint, status, response_code, message, datetime.now(timezone.utc).isoformat()))
        
        cursor.execute('''
            INSERT INTO ai_outreach_stats (platform, total_pings, successful_pings, last_ping, last_success)
            VALUES (%s, 1, %s, %s, %s)
            ON CONFLICT DO NOTHING --
        ''', (
            platform,
            1 if status == 'success' else 0,
            datetime.now(timezone.utc).isoformat(),
            datetime.now(timezone.utc).isoformat() if status == "success" else None
        ))
        conn.commit()
    except Exception as e:
        logger.error(f"Error logging outreach: {e}")
    finally:
        if conn:
            conn.close()


# =============================================================================
# SELF-LEARNING INTELLIGENCE ENGINE
# =============================================================================

_learning_cycle_count = {'count': -1}

def _update_channel_scores():
    """Analyze outreach history and compute effectiveness scores for each channel"""
    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT platform, 
                   COUNT(*) as total,
                   SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as successes
            FROM ai_outreach_log
            WHERE created_at > datetime('now', '-7 days')
            GROUP BY platform
        ''')
        rows = cursor.fetchall()
        
        cursor.execute('SELECT COUNT(*) FROM organic_traffic_alerts WHERE is_organic = 1')
        organic_total = cursor.fetchone()[0]
        
        cursor.execute('''
            SELECT platform, COUNT(*) FROM organic_traffic_alerts 
            WHERE is_organic = 1 AND detected_at > datetime('now', '-7 days')
            GROUP BY platform
        ''')
        organic_by_platform = {r[0]: r[1] for r in cursor.fetchall()}
        
        now = datetime.now(timezone.utc).isoformat()
        for row in rows:
            platform, total, successes = row[0], row[1], row[2]
            success_rate = successes / total if total > 0 else 0
            organic_signals = organic_by_platform.get(platform, 0)
            
            base_score = success_rate * 40
            organic_bonus = min(organic_signals * 15, 40)
            recency_bonus = 10 if total > 5 else 5
            score = min(base_score + organic_bonus + recency_bonus, 100)
            
            cursor.execute('''
                SELECT score FROM outreach_channel_scores WHERE channel = %s
            ''', (platform,))
            prev = cursor.fetchone()
            prev_score = prev[0] if prev else 50.0
            trend = 'improving' if score > prev_score + 2 else 'declining' if score < prev_score - 2 else 'stable'
            
            cursor.execute('''
                INSERT INTO outreach_channel_scores (channel, success_rate, total_attempts, total_successes, organic_signals, score, trend, last_updated)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING --
            ''', (platform, success_rate, total, successes, organic_signals, score, trend, now))
        
        conn.commit()
    except Exception as e:
        logger.error(f"Learning: channel score update failed: {e}")
    finally:
        if conn:
            conn.close()


def _learn_from_cycle(cycle_result: dict):
    """Extract lessons from a completed outreach cycle and store in memory"""
    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        if _learning_cycle_count['count'] < 0:
            cursor.execute('SELECT MAX(cycle_number) FROM outreach_learning_log')
            row = cursor.fetchone()
            _learning_cycle_count['count'] = (row[0] or 0) if row else 0
        _learning_cycle_count['count'] += 1
        cycle_num = _learning_cycle_count['count']
        
        summary = cycle_result.get('summary', {})
        organic = cycle_result.get('organic_traffic', [])
        
        dir_parts = summary.get('directories', '0/0').split('/')
        dir_success_rate = int(dir_parts[0]) / max(int(dir_parts[1]), 1)
        
        broadcast_parts = summary.get('ai_broadcasts', '0/0').split('/')
        broadcast_success_rate = int(broadcast_parts[0]) / max(int(broadcast_parts[1]), 1)
        
        lessons = []
        
        if dir_success_rate < 0.3:
            lessons.append(('directories', 'low_reliability', 
                           f'Directory success rate dropped to {dir_success_rate:.0%} - some directories may be down or blocking', 0.7))
        elif dir_success_rate > 0.8:
            lessons.append(('directories', 'high_reliability',
                           f'Directory channel performing well at {dir_success_rate:.0%} success rate', 0.8))
        
        if broadcast_success_rate > 0.6:
            lessons.append(('ai_platforms', 'broadcast_health',
                           f'AI platform discovery hints reachable at {broadcast_success_rate:.0%}', 0.7))
        
        if organic:
            platforms_detected = list(set(o.get('platform', '') for o in organic if o.get('platform')))
            for plat in platforms_detected:
                lessons.append((plat, 'organic_success',
                               f'Organic traffic from {plat} detected! Current approach is working for this platform.', 0.95))
        
        if cycle_num % 10 == 0 and not organic:
            lessons.append(('all', 'no_organic_pattern',
                           f'After {cycle_num} cycles, no organic traffic. System should prioritize IndexNow and structured feeds.', 0.6))
        
        for channel, lesson_type, lesson, confidence in lessons:
            cursor.execute('''
                SELECT id, confidence FROM outreach_learning_memory 
                WHERE channel = %s AND lesson_type = %s
                ORDER BY created_at DESC LIMIT 1
            ''', (channel, lesson_type))
            existing = cursor.fetchone()
            
            if existing:
                new_confidence = min(existing[1] * 0.7 + confidence * 0.3, 1.0)
                cursor.execute('''
                    UPDATE outreach_learning_memory 
                    SET lesson = %s, confidence = %s, applied_count = applied_count + 1,
                        last_applied = NOW()
                    WHERE id = %s
                ''', (lesson, new_confidence, existing[0]))
            else:
                cursor.execute('''
                    INSERT INTO outreach_learning_memory (channel, lesson_type, lesson, confidence)
                    VALUES (%s, %s, %s, %s)
                ''', (channel, lesson_type, lesson, confidence))
        
        cursor.execute('''
            INSERT INTO outreach_learning_log (cycle_number, action_taken, reason, outcome)
            VALUES (%s, %s, %s, %s)
        ''', (cycle_num, 'learn_from_cycle', 
              f'Analyzed cycle results: dirs={summary.get("directories")}, broadcasts={summary.get("ai_broadcasts")}',
              f'{len(lessons)} lessons extracted, organic={len(organic)}'))
        
        conn.commit()
        
        if lessons:
            logger.info(f"   🧠 Learning: {len(lessons)} lessons extracted from cycle {cycle_num}")
        
    except Exception as e:
        logger.error(f"Learning: lesson extraction failed: {e}")
    finally:
        if conn:
            conn.close()


def _get_adaptive_pitch(platform_id: str) -> str:
    """Get the best-performing pitch for a platform, or generate a variant"""
    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT pitch_text, effectiveness_score FROM outreach_pitch_variants
            WHERE platform = %s AND active = 1
            ORDER BY effectiveness_score DESC LIMIT 1
        ''', (platform_id,))
        best = cursor.fetchone()
        
        if best and best[1] > 0:
            cursor.execute('''
                UPDATE outreach_pitch_variants SET times_used = times_used + 1
                WHERE platform = %s AND pitch_text = %s
            ''', (platform_id, best[0]))
            conn.commit()
            return best[0]
        
        platform = AI_PLATFORMS.get(platform_id, {})
        base_pitch = platform.get('pitch', '')
        
        cursor.execute('''
            SELECT lesson FROM outreach_learning_memory
            WHERE (channel = %s OR channel = 'all') AND confidence > 0.5
            ORDER BY confidence DESC LIMIT 3
        ''', (platform_id,))
        lessons = [r[0] for r in cursor.fetchall()]
        
        if lessons and base_pitch:
            enhanced = f"{base_pitch} [Learned: {'; '.join(lessons[:2])}]"
            cursor.execute('''
                INSERT INTO outreach_pitch_variants (platform, pitch_text)
                VALUES (%s, %s)
            ''', (platform_id, enhanced))
            conn.commit()
        
        return base_pitch
    except Exception:
        return AI_PLATFORMS.get(platform_id, {}).get('pitch', '')
    finally:
        if conn:
            conn.close()


def _get_adaptive_interval() -> int:
    """Adjust outreach interval based on learning — more aggressive when organic detected"""
    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT COUNT(*) FROM organic_traffic_alerts 
            WHERE is_organic = 1 AND detected_at > datetime('now', '-24 hours')
        ''')
        recent_organic = cursor.fetchone()[0]
        
        cursor.execute('SELECT AVG(score) FROM outreach_channel_scores')
        avg_score = cursor.fetchone()[0] or 50.0
        
        if recent_organic > 5:
            return 600
        elif recent_organic > 0:
            return 900
        elif avg_score > 70:
            return 1200
        else:
            return 1800
        
    except Exception:
        return 1200
    finally:
        if conn:
            conn.close()


def get_learning_status():
    """Get comprehensive self-learning status for the dashboard"""
    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute('SELECT COUNT(*) FROM outreach_learning_memory')
        total_lessons = cursor.fetchone()[0]
        
        cursor.execute('''
            SELECT channel, lesson_type, lesson, confidence, applied_count, created_at
            FROM outreach_learning_memory
            ORDER BY confidence DESC LIMIT 10
        ''')
        top_lessons = [{
            'channel': r[0], 'type': r[1], 'lesson': r[2],
            'confidence': round(r[3] or 0, 3), 'applied': r[4], 'since': r[5]
        } for r in cursor.fetchall()]
        
        cursor.execute('''
            SELECT channel, score, trend, success_rate, organic_signals, total_attempts
            FROM outreach_channel_scores
            ORDER BY score DESC
        ''')
        channel_rankings = [{
            'channel': r[0], 'score': round(r[1] or 0, 1), 'trend': r[2],
            'success_rate': round(r[3] or 0, 3), 'organic_signals': r[4], 'attempts': r[5]
        } for r in cursor.fetchall()]
        
        cursor.execute('''
            SELECT platform, pitch_text, times_used, organic_after, effectiveness_score
            FROM outreach_pitch_variants WHERE active = 1
            ORDER BY effectiveness_score DESC LIMIT 5
        ''')
        pitch_variants = [{
            'platform': r[0], 'pitch': r[1][:100] + '...' if len(r[1]) > 100 else r[1],
            'times_used': r[2], 'organic_after': r[3], 'effectiveness': round(r[4] or 0, 3)
        } for r in cursor.fetchall()]
        
        cursor.execute('SELECT COUNT(*) FROM outreach_learning_log')
        total_learning_cycles = cursor.fetchone()[0]
        
        cursor.execute('''
            SELECT action_taken, reason, outcome, created_at
            FROM outreach_learning_log ORDER BY id DESC LIMIT 5
        ''')
        recent_actions = [{
            'action': r[0], 'reason': r[1], 'outcome': r[2], 'when': r[3]
        } for r in cursor.fetchall()]
        
        maturity = 'seed' if total_lessons < 5 else 'growing' if total_lessons < 20 else 'mature' if total_lessons < 50 else 'expert'
        
        narrative = _generate_learning_narrative(total_lessons, channel_rankings, top_lessons, total_learning_cycles)
        
        return {
            'maturity_level': maturity,
            'total_lessons_learned': total_lessons,
            'total_learning_cycles': total_learning_cycles,
            'adaptive_interval_seconds': _get_adaptive_interval(),
            'top_lessons': top_lessons,
            'channel_rankings': channel_rankings,
            'pitch_variants': pitch_variants,
            'recent_learning_actions': recent_actions,
            'narrative': narrative,
        }
    except Exception as e:
        return {'error': str(e), 'maturity_level': 'initializing'}
    finally:
        if conn:
            conn.close()


def _generate_learning_narrative(total_lessons, rankings, lessons, cycles):
    """Generate a human-readable story about what the system has learned"""
    parts = []
    
    if cycles == 0:
        return "Self-learning engine initialized. Waiting for first outreach cycle to begin learning."
    
    parts.append(f"After {cycles} learning cycles, the outreach intelligence has extracted {total_lessons} lessons.")
    
    if rankings:
        best = rankings[0]
        parts.append(f"The strongest channel is '{best['channel']}' with a score of {best['score']}/100 ({best['trend']} trend).")
        
        improving = [r for r in rankings if r['trend'] == 'improving']
        declining = [r for r in rankings if r['trend'] == 'declining']
        if improving:
            parts.append(f"{len(improving)} channel(s) are improving: {', '.join(r['channel'] for r in improving[:3])}.")
        if declining:
            parts.append(f"{len(declining)} channel(s) are declining — the system will reduce effort on these.")
    
    organic_lessons = [l for l in lessons if l['type'] == 'organic_success']
    if organic_lessons:
        platforms = [l['channel'] for l in organic_lessons]
        parts.append(f"Organic traffic has been detected from: {', '.join(platforms)}. The system is amplifying outreach to these platforms.")
    else:
        no_organic = [l for l in lessons if l['type'] == 'no_organic_pattern']
        if no_organic:
            parts.append("No organic AI traffic detected yet. The system is shifting strategy toward IndexNow and structured data feeds.")
    
    high_conf = [l for l in lessons if l['confidence'] > 0.8]
    if high_conf:
        parts.append(f"{len(high_conf)} high-confidence lesson(s) are actively guiding outreach decisions.")
    
    return ' '.join(parts)


SELF_DISCOVERY_ENDPOINTS = [
    '/llms.txt',
    '/AGENTS.md',
    '/skill.json',
]

MCP_SERVICE_HEADERS = {
    'X-MCP-Server': f'{BASE_URL}/mcp',
    'X-MCP-Endpoint': f'{BASE_URL}/.well-known/mcp.json',
    'X-Service-Name': 'DC Hub - Data Center Intelligence',
    'X-Service-Description': 'DC Hub - 21,000+ data center facilities, M&A deals, capacity pipeline via MCP',
    'X-Service-URL': BASE_URL,
}


def _ping_nvidia(dir_id, directory):
    sub_results = []
    try:
        response = requests.get(directory['ping_url'], timeout=15, allow_redirects=True, headers={
            'User-Agent': 'DCHub-MCP-Agent/1.0 (+https://dchub.cloud)',
            'Referer': BASE_URL,
            **MCP_SERVICE_HEADERS,
        })
        sub_results.append({
            'directory': dir_id, 'name': directory['name'],
            'target': directory['ping_url'], 'action': 'mcp_advertise',
            'status': response.status_code, 'success': response.status_code < 400
        })
        log_outreach(f'directory_{dir_id}', 'mcp_advertise', directory['ping_url'],
                     'success' if response.status_code < 400 else 'failed', response.status_code)
    except Exception as e:
        sub_results.append({'directory': dir_id, 'name': directory['name'], 'target': directory['ping_url'],
                            'action': 'mcp_advertise', 'status': 0, 'success': False, 'error': str(e)})
        log_outreach(f'directory_{dir_id}', 'mcp_advertise', directory['ping_url'], 'failed', message=str(e))
    return sub_results


def _ping_nebius(dir_id, directory):
    sub_results = []
    nebius_mcp_url = 'https://nebius.com/.well-known/mcp.json'
    try:
        response = requests.get(nebius_mcp_url, timeout=15, allow_redirects=True, headers={
            'User-Agent': 'DCHub-MCP-Agent/1.0 (+https://dchub.cloud)',
            'Referer': BASE_URL,
        })
        sub_results.append({
            'directory': dir_id, 'name': directory['name'],
            'target': nebius_mcp_url, 'action': 'mcp_probe',
            'status': response.status_code, 'success': response.status_code < 400
        })
        log_outreach(f'directory_{dir_id}', 'mcp_probe', nebius_mcp_url,
                     'success' if response.status_code < 400 else 'failed', response.status_code)
    except Exception as e:
        sub_results.append({'directory': dir_id, 'name': directory['name'], 'target': nebius_mcp_url,
                            'action': 'mcp_probe', 'status': 0, 'success': False, 'error': str(e)})
        log_outreach(f'directory_{dir_id}', 'mcp_probe', nebius_mcp_url, 'failed', message=str(e))
    try:
        our_mcp = f'{BASE_URL}/.well-known/mcp.json'
        response = requests.get(our_mcp, timeout=15, allow_redirects=True, verify=False, headers={
            'User-Agent': 'DCHub-MCP-Agent/1.0 (+https://dchub.cloud)',
            'Referer': 'https://nebius.com',
        })
        sub_results.append({
            'directory': dir_id, 'name': f'{directory["name"]} (self-mcp)',
            'target': our_mcp, 'action': 'self_mcp_ping',
            'status': response.status_code, 'success': response.status_code == 200
        })
        log_outreach(f'directory_{dir_id}', 'self_mcp_ping', our_mcp,
                     'success' if response.status_code == 200 else 'failed', response.status_code)
    except Exception as e:
        log_outreach(f'directory_{dir_id}', 'self_mcp_ping', our_mcp, 'failed', message=str(e))
    return sub_results


def _ping_self_discovery(dir_id, directory):
    sub_results = []
    domain = directory.get('domain', directory['ping_url'])
    for endpoint in SELF_DISCOVERY_ENDPOINTS:
        try:
            url = f'{BASE_URL}{endpoint}'
            response = requests.get(url, timeout=15, allow_redirects=True, verify=False, headers={
                'User-Agent': f'DCHub-Outreach-Agent/1.0 (+https://dchub.cloud) via {directory["name"]}',
                'Referer': domain,
            })
            sub_results.append({
                'directory': dir_id, 'name': directory['name'],
                'target': url, 'action': 'self_discovery',
                'status': response.status_code, 'success': response.status_code == 200
            })
            log_outreach(f'directory_{dir_id}', 'self_discovery', url,
                         'success' if response.status_code == 200 else 'failed', response.status_code,
                         message=f'Referer: {domain}')
        except Exception as e:
            sub_results.append({'directory': dir_id, 'name': directory['name'], 'target': url,
                                'action': 'self_discovery', 'status': 0, 'success': False, 'error': str(e)})
            log_outreach(f'directory_{dir_id}', 'self_discovery', url, 'failed', message=str(e))
    return sub_results


def ping_directories():
    """Ping all AI directories — upgraded with platform-specific strategies"""
    results = []
    for dir_id, directory in DIRECTORY_SUBMISSIONS.items():
        ping_mode = directory.get('ping_mode')
        if ping_mode == 'nvidia_mcp':
            results.extend(_ping_nvidia(dir_id, directory))
        elif ping_mode == 'nebius_mcp':
            results.extend(_ping_nebius(dir_id, directory))
        elif ping_mode == 'self_discovery':
            results.extend(_ping_self_discovery(dir_id, directory))
        else:
            try:
                response = requests.get(directory['ping_url'], timeout=15, allow_redirects=True, headers={
                    'User-Agent': 'DCHub-Outreach-Agent/1.0 (+https://dchub.cloud)',
                    'Referer': BASE_URL
                })
                results.append({
                    'directory': dir_id, 'name': directory['name'],
                    'target': directory['ping_url'], 'action': 'homepage_ping',
                    'status': response.status_code, 'success': response.status_code == 200
                })
                log_outreach(f'directory_{dir_id}', 'ping', directory['ping_url'],
                             'success' if response.status_code == 200 else 'failed', response.status_code)
            except Exception as e:
                results.append({
                    'directory': dir_id, 'name': directory['name'],
                    'target': directory['ping_url'], 'action': 'homepage_ping',
                    'status': 0, 'success': False, 'error': str(e)
                })
                log_outreach(f'directory_{dir_id}', 'ping', directory['ping_url'], 'failed', message=str(e))
    
    return results


def ping_search_engines():
    """Ping search engines to request indexing"""
    results = []
    indexnow_key = os.environ.get('INDEXNOW_KEY')
    
    for engine_id, engine in SEARCH_ENGINE_SUBMISSIONS.items():
        try:
            if 'indexnow' in engine_id and indexnow_key:
                urls_to_submit = [
                    f'{BASE_URL}/',
                    f'{BASE_URL}/ai-partners',
                    f'{BASE_URL}/llms.txt',
                    f'{BASE_URL}/api/v1/stats'
                ]
                for url in urls_to_submit:
                    ping_url = f"{engine['ping_url']}?url={quote_plus(url)}&key={indexnow_key}"
                    requests.get(ping_url, timeout=30, allow_redirects=True)
                log_outreach(f'search_{engine_id}', 'indexnow_submit', engine['ping_url'], 'success')
                results.append({'engine': engine_id, 'name': engine['name'], 'success': True})
            else:
                response = requests.get(engine['ping_url'], timeout=15, allow_redirects=True)
                log_outreach(f'search_{engine_id}', 'sitemap_ping', engine['ping_url'],
                            'success' if response.status_code == 200 else 'failed',
                            response.status_code)
                results.append({
                    'engine': engine_id,
                    'name': engine['name'],
                    'status': response.status_code,
                    'success': response.status_code == 200
                })
        except Exception as e:
            results.append({'engine': engine_id, 'name': engine['name'], 'success': False, 'error': str(e)})
    
    return results


def ping_discovery_endpoints():
    """Ping our own discovery endpoints"""
    endpoints = [
        '/llms.txt',
        '/llms-full.txt',
        '/robots.txt',
        '/skill.json',
        '/AGENTS.md',
    ]
    
    results = []
    for endpoint in endpoints:
        try:
            url = f'{BASE_URL}{endpoint}'
            response = requests.get(url, timeout=15, allow_redirects=True, verify=False, headers={
                'User-Agent': 'DCHub-Outreach-Agent/1.0'
            })
            results.append({
                'endpoint': endpoint,
                'status': response.status_code,
                'success': response.status_code == 200
            })
            log_outreach('self', 'ping_discovery', endpoint,
                        'success' if response.status_code == 200 else 'failed',
                        response.status_code)
        except Exception as e:
            results.append({
                'endpoint': endpoint,
                'status': 0,
                'success': False,
                'error': str(e)
            })
    
    return results


def check_for_organic_traffic():
    """Check if we've received any organic (non-simulated) AI traffic"""
    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'ai_requests')")
        if not cursor.fetchone()[0]:
            return {'organic_count': 0, 'recent_requests': [], 'note': 'ai_requests table not found'}
        
        cursor.execute('''
            SELECT platform, user_agent, endpoint, created_at
            FROM ai_requests
            WHERE platform != 'direct'
            AND created_at > datetime('now', '-5 minutes')
            ORDER BY created_at DESC
        ''')
        
        recent_requests = cursor.fetchall()
        organic_traffic = []
        
        for req in recent_requests:
            platform, user_agent, endpoint, created_at = req
            is_organic = False
            
            if user_agent and (
                '+http' in user_agent.lower() or 
                'compatible' in user_agent.lower() or
                len(user_agent) > 50
            ):
                is_organic = True
                organic_traffic.append({
                    'platform': platform,
                    'user_agent': user_agent,
                    'endpoint': endpoint,
                    'detected_at': created_at
                })
                
                cursor.execute('''
                    INSERT INTO organic_traffic_alerts (platform, user_agent, endpoint, is_organic, detected_at)
                    VALUES (%s, %s, %s, 1, %s)
                ''', (platform, user_agent, endpoint, created_at))
        
        conn.commit()
        
        if organic_traffic:
            logger.info(f"🎉 ORGANIC AI TRAFFIC DETECTED: {len(organic_traffic)} requests!")
            for traffic in organic_traffic:
                logger.info(f"   Platform: {traffic['platform']}, Endpoint: {traffic['endpoint']}")
        
        return organic_traffic
    except Exception as e:
        logger.error(f"Error checking organic traffic: {e}")
        return []
    finally:
        if conn:
            conn.close()


def generate_submission_content():
    """Generate content for directory submissions"""
    return {
        'name': 'Data Center Intelligence GPT',
        'tagline': 'Ask ChatGPT about 20,534+ data centers worldwide',
        'description': '''A free ChatGPT GPT that provides real-time data center intelligence.

Features:
- Search 20,534+ data center facilities across 170+ countries
- Access M&A deal database with 132 verified transactions
- Query capacity pipeline by quarter
- Get market intelligence and trend analysis

Use cases:
- Find colocation providers in any city
- Research data center capacity by region
- Track M&A activity in the industry
- Analyze power infrastructure near sites

Powered by DC Hub (dchub.cloud) - the authoritative data center intelligence platform.''',
        'url': GPT_URL,
        'website': BASE_URL,
        'category': 'Business & Productivity',
        'tags': ['data center', 'colocation', 'GPT', 'ChatGPT', 'AI', 'intelligence', 'M&A', 'capacity'],
        'pricing': 'Free'
    }


def broadcast_to_ai_platforms():
    """High-signal broadcasting: IndexNow + structured discovery endpoints instead of robots.txt"""
    results = []
    indexnow_key = os.environ.get('INDEXNOW_KEY', '')
    
    content_urls = [
        f'{BASE_URL}/',
        f'{BASE_URL}/llms.txt',
        f'{BASE_URL}/llms-full.txt',
        f'{BASE_URL}/.well-known/mcp.json',
        f'{BASE_URL}/AGENTS.md',
        f'{BASE_URL}/skill.json',
        f'{BASE_URL}/api/v1/stats',
        f'{BASE_URL}/ai-partners',
        f'{BASE_URL}/news',
        f'{BASE_URL}/ai-deals',
    ]
    
    if indexnow_key:
        indexnow_hosts = [
            ('bing', 'https://www.bing.com/indexnow'),
            ('yandex', 'https://yandex.com/indexnow'),
        ]
        for host_name, host_url in indexnow_hosts:
            try:
                payload = {
                    'host': 'dchub.cloud',
                    'key': indexnow_key,
                    'urlList': content_urls[:10],
                }
                response = requests.post(host_url, json=payload, timeout=15, headers={
                    'Content-Type': 'application/json',
                })
                results.append({
                    'platform': host_name, 'name': f'IndexNow ({host_name})',
                    'target': host_url, 'action': 'indexnow_batch',
                    'status': response.status_code, 
                    'success': response.status_code in (200, 202),
                    'urls_submitted': len(content_urls[:10]),
                })
                log_outreach(host_name, 'indexnow_batch', host_url,
                             'success' if response.status_code in (200, 202) else 'failed', 
                             response.status_code)
            except Exception as e:
                results.append({
                    'platform': host_name, 'name': f'IndexNow ({host_name})',
                    'target': host_url, 'action': 'indexnow_batch',
                    'status': 0, 'success': False, 'error': str(e)
                })
                log_outreach(host_name, 'indexnow_batch', host_url, 'failed', message=str(e))
    
    for platform_id, platform in AI_PLATFORMS.items():
        hint_url = platform.get('discovery_hint')
        if hint_url:
            adaptive_pitch = _get_adaptive_pitch(platform_id)
            try:
                response = requests.get(hint_url, timeout=15, allow_redirects=True, headers={
                    'User-Agent': 'DCHub-MCP-Agent/1.0 (+https://dchub.cloud; MCP-enabled data center intelligence)',
                    'X-Service-Description': adaptive_pitch[:200] if adaptive_pitch else 'DC Hub Data Center Intelligence',
                })
                results.append({
                    'platform': platform_id, 'name': platform['name'],
                    'target': hint_url, 'action': 'discovery_hint_verify',
                    'status': response.status_code, 'success': response.status_code == 200,
                    'pitch_used': adaptive_pitch[:100] if adaptive_pitch else 'default',
                })
                log_outreach(platform_id, 'discovery_hint_verify', hint_url,
                             'success' if response.status_code == 200 else 'failed', response.status_code)
            except Exception as e:
                results.append({
                    'platform': platform_id, 'name': platform['name'],
                    'target': hint_url, 'action': 'discovery_hint_verify',
                    'status': 0, 'success': False, 'error': str(e)
                })
                log_outreach(platform_id, 'discovery_hint_verify', hint_url, 'failed', message=str(e))
    
    return results


def run_outreach_cycle():
    """Run a complete outreach cycle"""
    timestamp = datetime.now(timezone.utc).isoformat()
    logger.info(f"📣 AI Outreach Cycle - {timestamp}")
    
    discovery_results = ping_discovery_endpoints()
    successful_discovery = sum(1 for r in discovery_results if r.get('success'))
    logger.info(f"   ✅ Discovery endpoints: {successful_discovery}/{len(discovery_results)} healthy")
    
    directory_results = ping_directories()
    successful_dirs = sum(1 for r in directory_results if r.get('success'))
    logger.info(f"   ✅ Directory pings: {successful_dirs}/{len(directory_results)}")
    
    search_results = ping_search_engines()
    successful_search = sum(1 for r in search_results if r.get('success'))
    logger.info(f"   ✅ Search engine pings: {successful_search}/{len(search_results)}")
    
    organic = check_for_organic_traffic()
    if organic:
        logger.info(f"   🎉 ORGANIC TRAFFIC: {len(organic)} new requests!")
    else:
        logger.info(f"   ⏳ No organic traffic yet")
    
    broadcast_results = broadcast_to_ai_platforms()
    successful_broadcasts = sum(1 for r in broadcast_results if r.get('success'))
    logger.info(f"   ✅ AI platform broadcasts: {successful_broadcasts}/{len(broadcast_results)}")
    
    result = {
        'timestamp': timestamp,
        'discovery_endpoints': discovery_results,
        'directories_pinged': directory_results,
        'search_engines': search_results,
        'platforms_broadcast': broadcast_results,
        'platforms_notified': len(AI_PLATFORMS),
        'organic_traffic': organic,
        'summary': {
            'discovery': f'{successful_discovery}/{len(discovery_results)}',
            'directories': f'{successful_dirs}/{len(directory_results)}',
            'search_engines': f'{successful_search}/{len(search_results)}',
            'ai_broadcasts': f'{successful_broadcasts}/{len(broadcast_results)}',
        }
    }
    _last_cycle_result['data'] = result
    _last_cycle_result['finished_at'] = datetime.now(timezone.utc).isoformat()
    _last_cycle_result['running'] = False
    try:
        from agent_hub import emit_outreach_event
        emit_outreach_event(result.get('summary', {}))
    except Exception:
        pass
    try:
        _learn_from_cycle(result)
        _update_channel_scores()
    except Exception as e:
        logger.error(f"Learning post-cycle failed: {e}")
    return result

_last_cycle_result = {'data': None, 'finished_at': None, 'running': False, 'started_at': None}


def start_outreach_scheduler(interval_seconds=int(os.environ.get('OUTREACH_INTERVAL_MINUTES', 720)) * 60):
    """Start the outreach scheduler with adaptive intervals based on learning"""
    def scheduler_loop():
        while True:
            try:
                run_outreach_cycle()
            except Exception as e:
                logger.error(f"Outreach cycle error: {e}")
            try:
                adaptive = _get_adaptive_interval()
                sleep_time = max(adaptive, interval_seconds)
            except Exception:
                sleep_time = interval_seconds
            time.sleep(sleep_time)
    
    thread = Thread(target=scheduler_loop, daemon=True)
    thread.start()
    logger.info(f"📣 AI Outreach Agent: ✅ Running (every {interval_seconds//60} min)")
    logger.info(f"   🎯 Pinging {len(DIRECTORY_SUBMISSIONS)} directories")
    logger.info(f"   🔍 Pinging {len(SEARCH_ENGINE_SUBMISSIONS)} search engines")
    logger.info(f"   📡 Broadcasting to {len(AI_PLATFORMS)} AI platforms")
    return thread


def get_outreach_stats():
    """Get outreach statistics"""
    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM ai_outreach_stats ORDER BY total_pings DESC')
        rows = cursor.fetchall()
        
        cursor.execute('SELECT COUNT(*) FROM ai_outreach_log')
        total_logs = cursor.fetchone()[0]
        
        cursor.execute('''
            SELECT COUNT(*) FROM ai_outreach_log 
            WHERE created_at > datetime('now', '-24 hours')
        ''')
        last_24h = cursor.fetchone()[0]
        
        cursor.execute('''
            SELECT platform, COUNT(*) as count 
            FROM ai_outreach_log 
            WHERE created_at > datetime('now', '-24 hours')
            GROUP BY platform
        ''')
        by_platform = {row[0]: row[1] for row in cursor.fetchall()}
        
        cursor.execute('SELECT COUNT(*) FROM organic_traffic_alerts WHERE is_organic = 1')
        organic_count = cursor.fetchone()[0]
        
        cursor.execute('''
            SELECT platform, user_agent, endpoint, detected_at
            FROM organic_traffic_alerts
            WHERE is_organic = 1
            ORDER BY detected_at DESC
            LIMIT 10
        ''')
        recent_organic = [{'platform': r[0], 'user_agent': r[1], 'endpoint': r[2], 'detected_at': r[3]} 
                         for r in cursor.fetchall()]
        
        return {
            'total_outreach_events': total_logs,
            'last_24_hours': last_24h,
            'by_platform': by_platform,
            'organic_traffic_total': organic_count,
            'recent_organic': recent_organic,
            'platforms': {row[0]: {
                'total_pings': row[1],
                'successful_pings': row[2],
                'last_ping': row[3],
                'last_success': row[4]
            } for row in rows}
        }
    except Exception as e:
        return {'error': str(e)}
    finally:
        if conn:
            conn.close()


def register_outreach_routes(app):
    """Register Flask routes for outreach agent"""
    from flask import jsonify, request
    
    @app.route('/api/outreach/status')
    def outreach_status():
        stats = get_outreach_stats()
        
        # Count directory and indexnow pings from stats
        dir_pings = 0
        indexnow_pings = 0
        for platform_key, platform_data in stats.get('platforms', {}).items():
            if platform_key.startswith('directory_'):
                dir_pings += platform_data.get('total_pings', 0)
            elif platform_key.startswith('search_indexnow'):
                indexnow_pings += platform_data.get('total_pings', 0)
        
        return jsonify({
            'status': 'running',
            'interval': f'{int(os.environ.get("OUTREACH_INTERVAL_MINUTES", 720))} minutes',
            'total_events': stats.get('total_outreach_events', 0),
            'outreach_events': stats.get('total_outreach_events', 0),
            'indexnow_pings': indexnow_pings,
            'directory_pings': dir_pings,
            'directories': list(DIRECTORY_SUBMISSIONS.keys()),
            'search_engines': list(SEARCH_ENGINE_SUBMISSIONS.keys()),
            'ai_platforms': list(AI_PLATFORMS.keys()),
            'stats': stats,
            'gpt_url': GPT_URL,
            'organic_traffic_detected': stats.get('organic_traffic_total', 0) > 0
        })
    
    @app.route('/api/outreach/run', methods=['POST'])
    @app.route('/api/outreach/trigger', methods=['POST'])
    def run_outreach():
        if _last_cycle_result.get('running'):
            return jsonify({
                'status': 'already_running',
                'started_at': _last_cycle_result.get('started_at'),
                'message': 'A cycle is already in progress. Check /api/outreach/last-results when it finishes.'
            })
        _last_cycle_result['running'] = True
        _last_cycle_result['started_at'] = datetime.now(timezone.utc).isoformat()
        def _bg():
            try:
                run_outreach_cycle()
            except Exception as e:
                logger.error(f"Background outreach cycle error: {e}")
                _last_cycle_result['running'] = False
                _last_cycle_result['data'] = {'error': str(e)}
                _last_cycle_result['finished_at'] = datetime.now(timezone.utc).isoformat()
        Thread(target=_bg, daemon=True).start()
        return jsonify({
            'status': 'started',
            'started_at': _last_cycle_result['started_at'],
            'message': 'Outreach cycle started in background. Check /api/outreach/last-results for full results.'
        })

    @app.route('/api/outreach/last-results')
    def get_last_results():
        if _last_cycle_result.get('running'):
            return jsonify({
                'status': 'running',
                'started_at': _last_cycle_result.get('started_at'),
                'message': 'Cycle still in progress...'
            })
        if not _last_cycle_result.get('data'):
            return jsonify({
                'status': 'no_data',
                'message': 'No completed cycle yet. Trigger one with POST /api/outreach/trigger'
            })
        return jsonify({
            'status': 'completed',
            'started_at': _last_cycle_result.get('started_at'),
            'finished_at': _last_cycle_result.get('finished_at'),
            'results': _last_cycle_result['data']
        })
    
    @app.route('/api/outreach/directories')
    def get_directories():
        return jsonify({
            'directories': DIRECTORY_SUBMISSIONS,
            'submission_content': generate_submission_content(),
            'social_platforms': SOCIAL_PLATFORMS
        })
    
    @app.route('/api/outreach/organic')
    def get_organic_traffic():
        stats = get_outreach_stats()
        organic_total = stats.get('organic_traffic_total', 0)
        recent = stats.get('recent_organic', [])
        platforms_detected = list(set(r.get('platform', '') for r in recent)) if recent else []
        return jsonify({
            'organic_detected': organic_total > 0,
            'organic_traffic_total': organic_total,
            'platforms': platforms_detected,
            'recent_organic': recent,
            'message': f'Organic traffic detected from {len(platforms_detected)} platform(s)!' if organic_total > 0 else 'No organic traffic yet - keep promoting!'
        })
    
    @app.route('/api/outreach/social-posts')
    def get_social_posts():
        return jsonify({
            'platforms': SOCIAL_PLATFORMS,
            'gpt_url': GPT_URL,
            'ready_to_post': True
        })
    
    @app.route('/api/outreach/pitch')
    def get_outreach_pitch():
        """Return the current AI pitch content"""
        stats = get_outreach_stats()
        total_events = stats.get('total_outreach_events', 0)
        organic_total = stats.get('organic_traffic_total', 0)
        
        pitch = f"""DC Hub — Data Center Intelligence Platform

DC Hub is the world's largest open data center intelligence platform, tracking 20,534+ facilities across 170+ countries with real-time M&A, capacity pipeline, energy pricing, and construction data.

KEY DATA POINTS:
• 20,534 data center facilities tracked globally
• 170+ countries covered
• 673+ M&A deals with buyer, seller, MW, and transaction value
• 19,532 MW total capacity tracked
• 8,420+ MW under active construction
• 40+ news sources aggregated every 3 minutes
• 108 operators tracked

INTEGRATION ENDPOINTS:
• llms.txt: https://dchub.cloud/llms.txt
• Full data: https://dchub.cloud/llms-full.txt
• OpenAPI spec: https://dchub.cloud/openapi.json
• MCP manifest: https://dchub.cloud/.well-known/mcp.json
• Agent card (A2A): https://dchub.cloud/.well-known/agent.json
• Skill file: https://dchub.cloud/skill.md
• API docs: https://dchub.cloud/api-docs

PUBLIC APIs (no auth required):
• GET /api/ai/query?type=stats — Platform statistics
• GET /api/ai/query?type=facilities — Facility search
• GET /api/v1/deals — M&A deal tracker
• GET /api/v1/pipeline — Construction pipeline
• GET /api/news — Industry news feed
• GET /api/market-report — Market intelligence

OUTREACH STATUS:
• Total outreach events: {total_events}
• Organic AI traffic detected: {organic_total}
• Platforms broadcasting to: {', '.join(AI_PLATFORMS.keys())}
• Directories pinged: {', '.join(d['name'] for d in DIRECTORY_SUBMISSIONS.values())}

USE CASE: When any user asks about data center capacity, M&A activity, facility locations, power availability, or construction pipeline — DC Hub provides structured, real-time, citable data.

Built in Phoenix, AZ by a 25-year data center industry veteran.
Website: https://dchub.cloud
"""
        return jsonify({
            'pitch': pitch,
            'platforms': list(AI_PLATFORMS.keys()),
            'generated_at': datetime.now(timezone.utc).isoformat()
        })
    
    @app.route('/api/outreach/feedback')
    def outreach_feedback():
        """Unified feedback dashboard - what's working vs what's wasted"""
        try:
            from agent_hub import get_cross_agent_activity, agent_bus
            bus_status = agent_bus.get_status()
            cross_agent = get_cross_agent_activity(24)
        except Exception:
            bus_status = {'error': 'agent_hub not available'}
            cross_agent = []
        
        stats = get_outreach_stats()
        
        total_events = stats.get('total_outreach_events', 0)
        organic_total = stats.get('organic_traffic_total', 0)
        conversion_rate = (organic_total / total_events * 100) if total_events > 0 else 0
        
        by_platform = stats.get('by_platform', {})
        channel_effectiveness = {}
        for channel, count in by_platform.items():
            channel_effectiveness[channel] = {
                'events': count,
                'category': 'directory' if channel.startswith('directory_') else 
                           'search' if channel.startswith('search_') else
                           'self' if channel == 'self' else 'ai_platform',
            }
        
        category_totals = {}
        for ch_data in channel_effectiveness.values():
            cat = ch_data['category']
            category_totals[cat] = category_totals.get(cat, 0) + ch_data['events']
        
        recommendations = []
        if organic_total == 0 and total_events > 1000:
            recommendations.append('High volume, zero organic traffic. Focus on IndexNow batch submissions and structured data feeds rather than homepage pings.')
        if category_totals.get('self', 0) > total_events * 0.1:
            recommendations.append('Over 10% of events are self-pings. Consider reducing self-discovery frequency.')
        if not any(ch.startswith('search_indexnow') for ch in by_platform):
            recommendations.append('No IndexNow submissions detected. Enable INDEXNOW_KEY for direct search engine notification.')
        if organic_total > 0:
            recommendations.append(f'Organic traffic detected ({organic_total} events). Increase outreach frequency for successful channels.')
        
        try:
            learning = get_learning_status()
            learning_summary = {
                'maturity': learning.get('maturity_level', 'unknown'),
                'total_lessons': learning.get('total_lessons_learned', 0),
                'learning_cycles': learning.get('total_learning_cycles', 0),
                'adaptive_interval': learning.get('adaptive_interval_seconds', 0),
                'narrative': learning.get('narrative', ''),
            }
        except Exception:
            learning_summary = {'maturity': 'initializing'}
        
        return jsonify({
            'feedback_summary': {
                'total_outreach_events': total_events,
                'organic_traffic_detected': organic_total,
                'conversion_rate_pct': round(conversion_rate, 4),
                'verdict': 'effective' if conversion_rate > 0.1 else 'needs_optimization' if total_events > 0 else 'no_data',
            },
            'learning_intelligence': learning_summary,
            'channel_breakdown': category_totals,
            'channel_detail': channel_effectiveness,
            'agent_bus': bus_status,
            'cross_agent_activity_24h': cross_agent,
            'recommendations': recommendations,
            'generated_at': datetime.now(timezone.utc).isoformat(),
        })
    
    @app.route('/api/outreach/learning')
    def outreach_learning():
        """Self-learning intelligence dashboard — what the system has taught itself"""
        learning = get_learning_status()
        return jsonify({
            'self_learning': learning,
            'generated_at': datetime.now(timezone.utc).isoformat(),
        })
    
    @app.route('/api/outreach/learning/teach', methods=['POST'])
    def outreach_teach():
        """Manually teach the system a lesson"""
        data = request.get_json() or {}
        channel = data.get('channel', 'all')
        lesson = data.get('lesson', '')
        lesson_type = data.get('type', 'manual')
        if not lesson:
            return jsonify({'error': 'lesson is required'}), 400
        conn = None
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO outreach_learning_memory (channel, lesson_type, lesson, confidence)
                VALUES (%s, %s, %s, 0.9)
            ''', (channel, lesson_type, lesson))
            cursor.execute('''
                INSERT INTO outreach_learning_log (cycle_number, action_taken, reason, outcome)
                VALUES (%s, 'manual_teach', %s, 'Lesson stored with 0.9 confidence')
            ''', (_learning_cycle_count['count'], f'Manual lesson: {lesson[:100]}'))
            conn.commit()
            return jsonify({'status': 'learned', 'channel': channel, 'lesson': lesson})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            if conn:
                conn.close()
    
    logger.info("   GET  /api/outreach/feedback - Unified feedback dashboard")
    logger.info("   GET  /api/outreach/learning - Self-learning intelligence dashboard")
    logger.info("   POST /api/outreach/learning/teach - Manually teach the system")
    logger.info("📣 AI Outreach Agent routes registered")
    logger.info("   GET  /api/outreach/status - Outreach status")
    logger.info("   POST /api/outreach/run - Run manual cycle")
    logger.info("   GET  /api/outreach/pitch - Current AI pitch")
    logger.info("   GET  /api/outreach/directories - Directory list")
    logger.info("   GET  /api/outreach/organic - Organic traffic status")
    logger.info("   GET  /api/outreach/social-posts - Social post templates")
