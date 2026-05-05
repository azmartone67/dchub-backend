"""
MCP Auto-Registration System
=============================
Automatically detects and registers new AI agents/platforms that hit
discovery or MCP endpoints. Parses User-Agent strings, deduplicates,
stores in SQLite, and injects into the platform-cards API response.
"""

import re
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse
from db_utils import get_db

logger = logging.getLogger(__name__)

KNOWN_BOT_PATTERNS = {
    'GPTBot':          {'name': 'OpenAI GPTBot',      'company': 'OpenAI',       'domain': 'openai.com',          'icon': '🟢', 'existing_id': 'chatgpt'},
    'ChatGPT-User':    {'name': 'ChatGPT User',       'company': 'OpenAI',       'domain': 'openai.com',          'icon': '🟢', 'existing_id': 'chatgpt'},
    'OAI-SearchBot':   {'name': 'OpenAI SearchBot',    'company': 'OpenAI',       'domain': 'openai.com',          'icon': '🟢', 'existing_id': 'chatgpt'},
    'ClaudeBot':       {'name': 'ClaudeBot',           'company': 'Anthropic',    'domain': 'anthropic.com',       'icon': '🟤', 'existing_id': 'claude'},
    'Claude-Web':      {'name': 'Claude Web',          'company': 'Anthropic',    'domain': 'anthropic.com',       'icon': '🟤', 'existing_id': 'claude'},
    'anthropic-ai':    {'name': 'Anthropic AI',        'company': 'Anthropic',    'domain': 'anthropic.com',       'icon': '🟤', 'existing_id': 'claude'},
    'PerplexityBot':   {'name': 'PerplexityBot',       'company': 'Perplexity',   'domain': 'perplexity.ai',       'icon': '🔵', 'existing_id': 'perplexity'},
    'Amazonbot':       {'name': 'Amazonbot',           'company': 'Amazon',       'domain': 'amazon.com',          'icon': 'Q',  'existing_id': 'amazon_q'},
    'Google-Extended':  {'name': 'Google AI Bot',      'company': 'Google',       'domain': 'google.com',          'icon': '🔷', 'existing_id': 'gemini'},
    'Googlebot':       {'name': 'Googlebot',           'company': 'Google',       'domain': 'google.com',          'icon': '🔷', 'existing_id': 'gemini'},
    'Google-InspectionTool': {'name': 'Google Inspect', 'company': 'Google',      'domain': 'google.com',          'icon': '🔷', 'existing_id': 'gemini'},
    'Gemini':          {'name': 'Google Gemini',        'company': 'Google',       'domain': 'gemini.google.com',   'icon': '🔷', 'existing_id': 'gemini'},
    'Bingbot':         {'name': 'Bingbot',             'company': 'Microsoft',    'domain': 'bing.com',            'icon': '🟣', 'existing_id': 'copilot'},
    'CopilotBot':      {'name': 'CopilotBot',          'company': 'Microsoft',    'domain': 'copilot.microsoft.com','icon': '🟣', 'existing_id': 'copilot'},
    'YouBot':          {'name': 'You.com Bot',          'company': 'You.com',      'domain': 'you.com',             'icon': '🔍', 'existing_id': 'youcom'},
    'PoeBot':          {'name': 'PoeBot',               'company': 'Quora',        'domain': 'poe.com',             'icon': '💜', 'existing_id': 'poe'},
    'cohere-ai':       {'name': 'Cohere AI',            'company': 'Cohere',       'domain': 'cohere.com',          'icon': '🔗', 'existing_id': 'rest_api'},
    'DeepSeekBot':     {'name': 'DeepSeek Bot',         'company': 'DeepSeek',     'domain': 'deepseek.com',        'icon': '🌊'},
    'Bytespider':      {'name': 'ByteDance Spider',     'company': 'ByteDance',    'domain': 'bytedance.com',       'icon': '🎵'},
    'DuckAssistBot':   {'name': 'DuckDuckGo AI',        'company': 'DuckDuckGo',   'domain': 'duckduckgo.com',      'icon': '🦆'},
    'CCBot':           {'name': 'Common Crawl',         'company': 'Common Crawl', 'domain': 'commoncrawl.org',     'icon': '📦'},
    'FacebookBot':     {'name': 'FacebookBot',          'company': 'Meta',         'domain': 'facebook.com',        'icon': 'M',  'existing_id': 'meta_ai'},
    'Meta-ExternalAgent': {'name': 'Meta AI Agent',     'company': 'Meta',         'domain': 'meta.ai',             'icon': 'M',  'existing_id': 'meta_ai'},
    'Applebot':        {'name': 'Applebot',             'company': 'Apple',        'domain': 'apple.com',           'icon': '🍎'},
    'Twitterbot':      {'name': 'Twitterbot',           'company': 'X Corp',       'domain': 'x.com',               'icon': '❌', 'existing_id': 'grok'},
    'ia_archiver':     {'name': 'Internet Archive',     'company': 'Internet Archive', 'domain': 'archive.org',     'icon': '📚'},
    'Timpibot':        {'name': 'Timpi Bot',            'company': 'Timpi',        'domain': 'timpi.io',            'icon': '⏱️'},
    'Scrapy':          {'name': 'Scrapy Crawler',       'company': 'Unknown',      'domain': 'scrapy.org',          'icon': '🕷️'},
}

MCP_CLIENT_PATTERNS = [
    'mcp-client', 'mcp-sdk', 'modelcontextprotocol',
    'cursor/', 'windsurf/', 'zed/', 'cline/',
    'continue/', 'sourcegraph',
]

SKIP_USER_AGENTS = [
    'DCHub-Outreach-Agent', 'DCHub-MCP-Agent',
    'python-requests', 'Go-http-client',
    'curl/', 'wget/', 'httpie/',
    'UptimeRobot', 'StatusCake', 'Pingdom',
    'health-check', 'monitoring',
]


def init_auto_register_db():
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS discovered_platforms (
                id SERIAL PRIMARY KEY,
                user_agent TEXT NOT NULL,
                first_seen TEXT DEFAULT (NOW()),
                last_seen TEXT DEFAULT (NOW()),
                request_count INTEGER DEFAULT 1,
                identified_as TEXT,
                protocol_guess TEXT,
                auto_configured INTEGER DEFAULT 0
            )
        ''')
        c = conn.cursor()
        c.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS idx_discovered_platforms_user_agent
            ON discovered_platforms(user_agent)
        ''')
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS discovery_events (
                id SERIAL PRIMARY KEY,
                platform_key TEXT NOT NULL,
                user_agent TEXT,
                referer TEXT,
                endpoint TEXT,
                ip_address TEXT,
                headers_json TEXT,
                created_at TEXT NOT NULL
            )
        ''')
        conn.commit()
    finally:
        conn.close()
    logger.info("   ✅ MCP Auto-Registration tables initialized")


def _parse_user_agent(ua_string):
    if not ua_string:
        return None

    ua_lower = ua_string.lower()
    for skip in SKIP_USER_AGENTS:
        if skip.lower() in ua_lower:
            return None

    for pattern, info in KNOWN_BOT_PATTERNS.items():
        if pattern.lower() in ua_lower:
            return {
                'name': info['name'],
                'company': info['company'],
                'domain': info['domain'],
                'icon': info['icon'],
                'is_known_bot': True,
                'existing_id': info.get('existing_id'),
                'match_pattern': pattern,
            }

    for mcp_pat in MCP_CLIENT_PATTERNS:
        if mcp_pat.lower() in ua_lower:
            client_name = ua_string.split('/')[0].strip() if '/' in ua_string else ua_string[:40]
            return {
                'name': f'MCP Client: {client_name}',
                'company': 'MCP Ecosystem',
                'domain': None,
                'icon': '🌐',
                'is_known_bot': False,
                'existing_id': None,
                'match_pattern': mcp_pat,
            }

    bot_match = re.search(r'(compatible;\s*(\w+Bot)[/\s])', ua_string, re.IGNORECASE)
    if bot_match:
        bot_name = bot_match.group(2)
        return {
            'name': bot_name,
            'company': 'Unknown',
            'domain': None,
            'icon': '🤖',
            'is_known_bot': False,
            'existing_id': None,
            'match_pattern': bot_name,
        }

    bot_match2 = re.search(r'^(\w+Bot)[/\s]', ua_string, re.IGNORECASE)
    if bot_match2:
        bot_name = bot_match2.group(1)
        return {
            'name': bot_name,
            'company': 'Unknown',
            'domain': None,
            'icon': '🤖',
            'is_known_bot': False,
            'existing_id': None,
            'match_pattern': bot_name,
        }

    if len(ua_string) < 30 or 'mozilla' in ua_lower:
        return None

    return {
        'name': 'Unknown Agent',
        'company': 'Unknown',
        'domain': None,
        'icon': '❓',
        'is_known_bot': False,
        'existing_id': None,
        'match_pattern': ua_string[:60],
    }


def _extract_domain_from_referer(referer):
    if not referer:
        return None
    try:
        parsed = urlparse(referer)
        domain = parsed.netloc or parsed.path
        domain = domain.replace('www.', '')
        if '.' in domain and len(domain) > 3:
            return domain
    except Exception:
        pass
    return None


def _make_platform_key(parsed_info, referer_domain):
    if parsed_info.get('existing_id'):
        return f"known_{parsed_info['existing_id']}"
    if parsed_info.get('match_pattern'):
        clean = re.sub(r'[^a-z0-9]', '_', parsed_info['match_pattern'].lower())
        return f"auto_{clean}"[:64]
    if referer_domain:
        clean = re.sub(r'[^a-z0-9]', '_', referer_domain.lower())
        return f"domain_{clean}"[:64]
    return f"unknown_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"


def detect_and_register(request_obj, endpoint_hit):
    try:
        ua_string = request_obj.headers.get('User-Agent', '')
        referer = request_obj.headers.get('Referer', '')

        parsed = _parse_user_agent(ua_string)
        if not parsed:
            return None

        referer_domain = _extract_domain_from_referer(referer)
        if referer_domain and not parsed.get('domain'):
            parsed['domain'] = referer_domain

        platform_key = _make_platform_key(parsed, referer_domain)
        now = datetime.now(timezone.utc).isoformat()
        identified_as = parsed.get('name', 'Unknown')
        method = 'mcp_client' if any(p in ua_string.lower() for p in MCP_CLIENT_PATTERNS) else 'user_agent'

        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO discovered_platforms
                (user_agent, first_seen, last_seen, request_count, identified_as, protocol_guess, auto_configured)
                VALUES (%s, %s, %s, 1, %s, %s, 0)
                ON CONFLICT (user_agent) DO UPDATE SET
                    request_count = discovered_platforms.request_count + 1,
                    last_seen = EXCLUDED.last_seen
            ''', (ua_string[:500], now, now, identified_as, method))
            conn.commit()
        finally:
            conn.close()

        cursor_check = None
        try:
            conn2 = get_db()
            cursor2 = conn2.cursor()
            cursor2.execute('SELECT id FROM discovered_platforms WHERE user_agent = %s', (ua_string[:500],))
            row = cursor2.fetchone()
            conn2.close()
            is_new = row is None
        except Exception:
            is_new = False

        return {'platform_key': platform_key, 'name': identified_as, 'is_new': is_new}

    except Exception as e:
        logger.error(f"Auto-register error: {e}")
        return None


def get_discovered_platforms_as_cards():
    try:
        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                SELECT id, user_agent, first_seen, last_seen, request_count, identified_as
                FROM discovered_platforms
                WHERE auto_configured = 0
                ORDER BY last_seen DESC
                LIMIT 50
            ''')
            rows = cursor.fetchall()
        finally:
            conn.close()

        cards = []
        for row in rows:
            (pid, ua, first_seen, last_seen, visits, identified_as) = row
            first_dt = str(first_seen)[:10] if first_seen else ''
            name = identified_as or 'Unknown Platform'
            cards.append({
                "id": str(pid),
                "category": "discovered",
                "name": name,
                "company": "Unknown",
                "logo_url": '',
                "icon": '🤖',
                "icon_bg": 'rgba(100,100,100,.12)',
                "card_class": "generic",
                "status": "DISCOVERED",
                "status_class": "status-discovered",
                "description": f"Auto-discovered on {first_dt}. {visits} visit(s). User-Agent: {(ua or '')[:80]}",
                "method": f"DETECTED: {visits} visits",
                "link_url": '',
                "link_text": '',
                "link_external": True,
                "sort_order": 100 + (999999 - (visits or 0)),
                "auto_discovered": True,
                "first_seen": str(first_seen) if first_seen else None,
                "last_seen": str(last_seen) if last_seen else None,
                "visit_count": visits or 0,
            })
        return cards
    except Exception as e:
        logger.error(f"Error loading discovered cards: {e}")
        return []


def get_all_discovered():
    try:
        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                SELECT id, user_agent, first_seen, last_seen, request_count,
                       identified_as, protocol_guess, auto_configured
                FROM discovered_platforms
                ORDER BY last_seen DESC
                LIMIT 100
            ''')
            rows = cursor.fetchall()
        finally:
            conn.close()

        platforms = []
        for row in rows:
            platforms.append({
                'platform_key': str(row[0]),
                'name': row[5] or 'Unknown',
                'company': '',
                'domain': '',
                'logo_url': '',
                'icon': '🤖',
                'status': 'discovered',
                'discovery_method': row[6] or 'auto',
                'visit_count': row[4] or 0,
                'first_seen': str(row[2]) if row[2] else None,
                'last_seen': str(row[3]) if row[3] else None,
                'user_agent': row[1] or '',
                'referer': '',
                'endpoint_hit': '',
                'is_known_bot': False,
                'existing_platform_id': '',
                'show_on_cards': not bool(row[7]),
            })
        return platforms
    except Exception as e:
        logger.error(f"Error loading discovered platforms: {e}")
        return []


def get_recent_events(limit=50):
    try:
        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                SELECT id, user_agent, first_seen, last_seen, request_count, identified_as
                FROM discovered_platforms
                ORDER BY last_seen DESC
                LIMIT %s
            ''', (limit,))
            rows = cursor.fetchall()
        finally:
            conn.close()
        return [
            {
                'platform_key': str(r[0]), 'user_agent': r[1] or '', 'referer': '',
                'endpoint': '', 'ip_address': '', 'created_at': str(r[3]) if r[3] else ''
            }
            for r in rows
        ]
    except Exception as e:
        logger.error(f"Error loading events: {e}")
        return []


def get_discovery_stats():
    try:
        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute('SELECT COUNT(*) FROM discovered_platforms')
            total = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM discovered_platforms WHERE auto_configured = 0')
            new_unknown = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM discovered_platforms WHERE auto_configured = 1')
            known = cursor.fetchone()[0]
        finally:
            conn.close()
        return {
            'total_platforms': total,
            'known_bots': known,
            'new_unknown': new_unknown,
            'total_events': total,
            'showing_on_cards': new_unknown,
        }
    except Exception as e:
        logger.error(f"Stats error: {e}")
        return {}
