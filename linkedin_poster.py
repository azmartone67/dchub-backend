"""
DC Hub LinkedIn Auto-Poster
============================
Automated daily LinkedIn posting for the DC Hub company page.
Pulls live data from DC Hub APIs, generates themed posts, and publishes via LinkedIn API.

Deploy on Replit with a daily cron job.

REQUIRED ENVIRONMENT VARIABLES (set in Replit Secrets):
  LINKEDIN_ACCESS_TOKEN    - OAuth2 token with w_organization_social scope
  LINKEDIN_ORG_ID          - DC Hub LinkedIn Organization ID (numeric)
  ANTHROPIC_API_KEY        - Claude API key for post generation
  DCHUB_API_BASE           - DC Hub API base URL (default: https://dchub.cloud)

OPTIONAL:
  POST_HOUR                - Hour to post in UTC (default: 14 = 7am MST)
  DRY_RUN                  - Set to "true" to generate without posting
"""

import os
import json
import random
import logging
import hashlib
from datetime import datetime, timedelta
from pathlib import Path

import requests

# ============================================================
# CONFIGURATION
# ============================================================
DCHUB_API_BASE = os.environ.get('DCHUB_API_BASE', 'https://dchub.cloud')
LINKEDIN_ACCESS_TOKEN = os.environ.get('LINKEDIN_ACCESS_TOKEN', '')
LINKEDIN_ORG_ID = os.environ.get('LINKEDIN_ORG_ID', '')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
DRY_RUN = os.environ.get('DRY_RUN', 'false').lower() == 'true'
POST_HOUR = int(os.environ.get('POST_HOUR', '14'))  # UTC

LOG_DIR = Path('logs')
LOG_DIR.mkdir(exist_ok=True)
HISTORY_FILE = Path('post_history.json')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_DIR / 'linkedin_poster.log'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger('linkedin-poster')

# ============================================================
# CONTENT THEMES — rotates daily across 5 themes
# ============================================================
THEMES = [
    {
        'id': 'ai_traction',
        'name': 'AI Platform Traction',
        'data_endpoints': ['/api/v1/stats', '/api/agent/stats'],
        'prompt_context': 'AI platform adoption stats, agent request volumes, new platform connections',
        'hashtags': ['#DataCenters', '#AI', '#AIAgents', '#DataCenterIntelligence', '#MCP', '#LLM'],
        'emoji': '🤖'
    },
    {
        'id': 'deals',
        'name': 'M&A Deals & Market Moves',
        'data_endpoints': ['/api/v1/deals'],
        'prompt_context': 'Recent M&A transactions, deal values, buyer/seller activity, market consolidation',
        'hashtags': ['#DataCenters', '#MergersAndAcquisitions', '#Infrastructure', '#DigitalInfrastructure', '#Investment'],
        'emoji': '💰'
    },
    {
        'id': 'pipeline',
        'name': 'New Facilities & Pipeline',
        'data_endpoints': ['/api/v1/pipeline', '/api/v1/stats'],
        'prompt_context': 'New data center construction, capacity expansions, power procurement, market growth',
        'hashtags': ['#DataCenters', '#Construction', '#CapacityPipeline', '#Hyperscale', '#PowerInfrastructure'],
        'emoji': '🏗️'
    },
    {
        'id': 'thought_leadership',
        'name': 'Industry Thought Leadership',
        'data_endpoints': ['/api/market-report', '/api/v1/stats'],
        'prompt_context': 'Industry trends, market analysis, power constraints, AI demand impact on data centers, sustainability',
        'hashtags': ['#DataCenters', '#DigitalInfrastructure', '#TechTrends', '#CloudComputing', '#Sustainability'],
        'emoji': '💡'
    },
    {
        'id': 'product_update',
        'name': 'DC Hub Product Updates',
        'data_endpoints': ['/api/v1/stats', '/api/agent/stats'],
        'prompt_context': 'DC Hub platform features, new capabilities, API updates, coverage milestones, user growth',
        'hashtags': ['#DataCenters', '#DCHub', '#PropTech', '#DataAnalytics', '#MarketIntelligence'],
        'emoji': '⚡'
    }
]

# ============================================================
# DATA FETCHING — pull live data from DC Hub APIs
# ============================================================
def fetch_dchub_data(endpoint):
    """Fetch data from a DC Hub API endpoint."""
    url = f"{DCHUB_API_BASE}{endpoint}"
    try:
        resp = requests.get(url, timeout=15, headers={'User-Agent': 'DCHub-LinkedIn-Poster/1.0'})
        if resp.status_code == 200:
            return resp.json()
        else:
            log.warning(f"API returned {resp.status_code} for {endpoint}")
            return None
    except Exception as e:
        log.error(f"Failed to fetch {endpoint}: {e}")
        return None


def gather_theme_data(theme):
    """Gather all data needed for a theme's post."""
    data = {}
    for endpoint in theme['data_endpoints']:
        result = fetch_dchub_data(endpoint)
        if result:
            data[endpoint] = result
    return data


# ============================================================
# POST GENERATION — Claude generates LinkedIn-optimized posts
# ============================================================
def generate_post(theme, live_data, day_of_year):
    """Use Claude to generate a LinkedIn post from live DC Hub data."""

    data_summary = json.dumps(live_data, indent=2, default=str)[:3000]  # Truncate for context window

    prompt = f"""You are the social media manager for DC Hub (dchub.cloud), the world's most comprehensive 
data center intelligence platform tracking 20,000+ facilities across 140+ countries.

Generate a LinkedIn post for the DC Hub company page. Today's theme: {theme['name']}

LIVE DATA FROM DC HUB APIs:
{data_summary}

THEME CONTEXT: {theme['prompt_context']}

POST REQUIREMENTS:
- LinkedIn company page post (professional but engaging)
- 150-280 words (LinkedIn sweet spot for engagement)
- Open with a hook — a surprising stat, bold claim, or question
- Include 1-2 specific data points from the live data above
- End with a clear CTA: visit dchub.cloud, try the API, or follow for more
- Include a relevant emoji or two naturally (don't overdo it)
- Write in first person plural ("we" = DC Hub team)
- Mention that DC Hub is used by 12+ AI platforms when relevant
- Sound like an industry insider, not a marketer
- DO NOT use hashtags (I'll add those separately)
- DO NOT use generic AI hype — be specific and data-driven
- Vary the format: some posts are short and punchy, some are mini-analyses, some ask questions
- Day variation seed: {day_of_year} (use this to vary your style)

TONE: Confident authority. DC Hub has the data. We're the source, not the commentary.

Return ONLY the post text, nothing else. No preamble, no quotes around it."""

    try:
        resp = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'Content-Type': 'application/json',
                'x-api-key': ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01'
            },
            json={
                'model': 'claude-sonnet-4-20250514',
                'max_tokens': 1024,
                'messages': [{'role': 'user', 'content': prompt}]
            },
            timeout=30
        )

        if resp.status_code == 200:
            result = resp.json()
            post_text = result['content'][0]['text'].strip()
            return post_text
        else:
            log.error(f"Claude API error {resp.status_code}: {resp.text[:200]}")
            return None

    except Exception as e:
        log.error(f"Post generation failed: {e}")
        return None


def format_final_post(post_text, theme):
    """Add hashtags and final formatting."""
    hashtags = ' '.join(theme['hashtags'])
    return f"{post_text}\n\n{hashtags}"


# ============================================================
# LINKEDIN POSTING — publish to company page via API
# ============================================================
def post_to_linkedin(text):
    """Publish a post to the DC Hub LinkedIn company page."""
    if DRY_RUN:
        log.info(f"[DRY RUN] Would post to LinkedIn:\n{text[:200]}...")
        return {'dry_run': True, 'success': True}

    url = 'https://api.linkedin.com/rest/posts'
    headers = {
        'Authorization': f'Bearer {LINKEDIN_ACCESS_TOKEN}',
        'Content-Type': 'application/json',
        'X-Restli-Protocol-Version': '2.0.0',
        'LinkedIn-Version': '202402'
    }

    payload = {
        'author': f'urn:li:organization:{LINKEDIN_ORG_ID}',
        'commentary': text,
        'visibility': 'PUBLIC',
        'distribution': {
            'feedDistribution': 'MAIN_FEED',
            'targetEntities': [],
            'thirdPartyDistributionChannels': []
        },
        'lifecycleState': 'PUBLISHED',
        'isReshareDisabledByAuthor': False
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=20)
        if resp.status_code in (200, 201):
            log.info("Successfully posted to LinkedIn")
            return {'success': True, 'status': resp.status_code, 'headers': dict(resp.headers)}
        else:
            log.error(f"LinkedIn API error {resp.status_code}: {resp.text[:500]}")
            return {'success': False, 'status': resp.status_code, 'error': resp.text[:500]}
    except Exception as e:
        log.error(f"LinkedIn posting failed: {e}")
        return {'success': False, 'error': str(e)}


# ============================================================
# POST HISTORY — track what's been posted to avoid repeats
# ============================================================
def load_history():
    """Load post history from JSON file."""
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text())
        except:
            return []
    return []


def save_to_history(entry):
    """Append a post to the history file."""
    history = load_history()
    history.append(entry)
    # Keep last 90 days of history
    cutoff = (datetime.utcnow() - timedelta(days=90)).isoformat()
    history = [h for h in history if h.get('timestamp', '') > cutoff]
    HISTORY_FILE.write_text(json.dumps(history, indent=2))


def get_post_hash(text):
    """Generate a hash of post content to detect duplicates."""
    return hashlib.md5(text.encode()).hexdigest()[:12]


# ============================================================
# THEME SELECTION — rotates through themes with variety
# ============================================================
def select_theme():
    """Select today's theme based on day rotation with some randomness."""
    today = datetime.utcnow()
    day_of_year = today.timetuple().tm_yday

    # Base rotation: cycle through 5 themes
    base_index = day_of_year % len(THEMES)

    # Check history to avoid repeating yesterday's theme
    history = load_history()
    if history:
        last_theme = history[-1].get('theme_id', '')
        if THEMES[base_index]['id'] == last_theme:
            # Shift to next theme
            base_index = (base_index + 1) % len(THEMES)

    return THEMES[base_index], day_of_year


# ============================================================
# MAIN EXECUTION
# ============================================================
def run():
    """Main execution — select theme, fetch data, generate post, publish."""
    log.info("=" * 60)
    log.info("DC Hub LinkedIn Auto-Poster — Starting daily run")
    log.info("=" * 60)

    # 1. Select today's theme
    theme, day_of_year = select_theme()
    log.info(f"Today's theme: {theme['emoji']} {theme['name']}")

    # 2. Fetch live data
    log.info("Fetching live data from DC Hub APIs...")
    live_data = gather_theme_data(theme)
    if not live_data:
        log.error("No data returned from any endpoint. Aborting.")
        return False

    log.info(f"Got data from {len(live_data)} endpoint(s)")

    # 3. Generate post
    log.info("Generating post via Claude...")
    post_text = generate_post(theme, live_data, day_of_year)
    if not post_text:
        log.error("Post generation failed. Aborting.")
        return False

    # 4. Format with hashtags
    final_post = format_final_post(post_text, theme)
    log.info(f"Generated post ({len(final_post)} chars):\n{final_post}")

    # 5. Check for duplicate content
    post_hash = get_post_hash(post_text)
    history = load_history()
    recent_hashes = [h.get('hash', '') for h in history[-30:]]
    if post_hash in recent_hashes:
        log.warning("Duplicate content detected. Regenerating...")
        post_text = generate_post(theme, live_data, day_of_year + 1000)
        if post_text:
            final_post = format_final_post(post_text, theme)
            post_hash = get_post_hash(post_text)

    # 6. Post to LinkedIn
    log.info("Publishing to LinkedIn...")
    result = post_to_linkedin(final_post)

    # 7. Save to history
    entry = {
        'timestamp': datetime.utcnow().isoformat(),
        'theme_id': theme['id'],
        'theme_name': theme['name'],
        'post_text': final_post,
        'hash': post_hash,
        'char_count': len(final_post),
        'result': result,
        'data_endpoints': list(live_data.keys())
    }
    save_to_history(entry)

    if result.get('success'):
        log.info("✅ Daily post completed successfully")
    else:
        log.error(f"❌ Post failed: {result}")

    return result.get('success', False)


# ============================================================
# FLASK ENDPOINTS — for manual triggers and monitoring
# ============================================================
def register_linkedin_routes(app):
    """Register LinkedIn poster routes with an existing Flask app."""
    from flask import jsonify, request as flask_request

    @app.route('/api/linkedin/post-now', methods=['POST'])
    def linkedin_post_now():
        """Manually trigger a LinkedIn post."""
        admin_key = flask_request.headers.get('X-Admin-Key', '')
        if admin_key != os.environ.get('ADMIN_KEY', ''):
            return jsonify({'error': 'Unauthorized'}), 401
        success = run()
        return jsonify({'success': success, 'timestamp': datetime.utcnow().isoformat()})

    @app.route('/api/linkedin/preview', methods=['GET'])
    def linkedin_preview():
        """Preview what today's post would look like without publishing."""
        theme, day_of_year = select_theme()
        live_data = gather_theme_data(theme)
        post_text = generate_post(theme, live_data, day_of_year)
        if post_text:
            final_post = format_final_post(post_text, theme)
            return jsonify({
                'theme': theme['name'],
                'post': final_post,
                'char_count': len(final_post),
                'data_sources': list(live_data.keys()),
                'preview_only': True
            })
        return jsonify({'error': 'Generation failed'}), 500

    @app.route('/api/linkedin/history', methods=['GET'])
    def linkedin_history():
        """View posting history."""
        history = load_history()
        return jsonify({
            'total_posts': len(history),
            'recent': history[-10:]  # Last 10 posts
        })

    @app.route('/api/linkedin/status', methods=['GET'])
    def linkedin_status():
        """Check poster status and configuration."""
        history = load_history()
        last_post = history[-1] if history else None
        return jsonify({
            'configured': bool(LINKEDIN_ACCESS_TOKEN and LINKEDIN_ORG_ID and ANTHROPIC_API_KEY),
            'dry_run': DRY_RUN,
            'themes': [t['name'] for t in THEMES],
            'total_posts': len(history),
            'last_post': {
                'timestamp': last_post['timestamp'] if last_post else None,
                'theme': last_post['theme_name'] if last_post else None,
                'success': last_post['result']['success'] if last_post else None
            } if last_post else None,
            'next_theme': select_theme()[0]['name']
        })


# ============================================================
# ENTRY POINT — run directly or import into Flask
# ============================================================
if __name__ == '__main__':
    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY not set. Cannot generate posts.")
        exit(1)
    if not LINKEDIN_ACCESS_TOKEN and not DRY_RUN:
        log.error("LINKEDIN_ACCESS_TOKEN not set and DRY_RUN is false. Cannot post.")
        exit(1)
    if not LINKEDIN_ORG_ID and not DRY_RUN:
        log.error("LINKEDIN_ORG_ID not set and DRY_RUN is false. Cannot post.")
        exit(1)

    run()
