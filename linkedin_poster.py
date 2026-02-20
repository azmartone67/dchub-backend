"""
DC Hub - LinkedIn Auto-Posting System
======================================
Auto-generate and schedule LinkedIn posts from DC Hub data.

Features:
- Generate posts from news/announcements
- Generate market update posts
- Generate facility milestone posts
- Schedule posts for optimal times
- Track post performance

Note: LinkedIn API requires OAuth authentication.
This module provides the backend; you'll need to:
1. Create a LinkedIn App at https://developer.linkedin.com
2. Set LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET env vars
3. Complete OAuth flow to get access token
"""

import os
import json
import hashlib
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from flask import Blueprint, request, jsonify
import requests
from db_utils import get_db

# Blueprint
linkedin_bp = Blueprint('linkedin', __name__)

DB_PATH = os.environ.get('DB_PATH', 'dc_nexus.db')

# LinkedIn API Config
LINKEDIN_CLIENT_ID = os.environ.get('LINKEDIN_CLIENT_ID', '')
LINKEDIN_CLIENT_SECRET = os.environ.get('LINKEDIN_CLIENT_SECRET', '')
LINKEDIN_ACCESS_TOKEN = os.environ.get('LINKEDIN_ACCESS_TOKEN', '')
LINKEDIN_ORG_ID = os.environ.get('LINKEDIN_ORG_ID', '')  # Company page ID

# =============================================================================
# DATABASE SETUP
# =============================================================================

def init_linkedin_db():
    """Initialize LinkedIn posts table"""
    conn = get_db()
    c = conn.cursor()
    
    c.execute("""
        CREATE TABLE IF NOT EXISTS linkedin_posts (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            post_type TEXT,
            source_id TEXT,
            status TEXT DEFAULT 'draft',
            scheduled_at TEXT,
            posted_at TEXT,
            linkedin_post_id TEXT,
            engagement TEXT,
            created_at TEXT
        )
    """)
    
    c.execute("""
        CREATE TABLE IF NOT EXISTS linkedin_schedule (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day_of_week INTEGER,
            hour INTEGER,
            enabled INTEGER DEFAULT 1
        )
    """)
    
    conn.commit()
    conn.close()

init_linkedin_db()

# =============================================================================
# POST GENERATION
# =============================================================================

POST_TEMPLATES = {
    'market_update': """📊 {market} Data Center Market Update

{stats}

Key Highlights:
{highlights}

What trends are you seeing in {market}? 👇

#DataCenters #CloudInfrastructure #{market_tag} #MarketIntelligence
""",
    
    'new_facility': """🏢 New Data Center Alert!

{provider} is expanding in {location} with a new facility:
• {power} MW planned capacity
• {sqft} sq ft footprint
• Expected completion: {timeline}

This brings the {market} market total to {total_facilities} facilities.

What does this mean for the region? Share your thoughts! 💬

#DataCenter #{provider_tag} #{market_tag} #Infrastructure
""",
    
    'weekly_digest': """📈 Weekly Data Center Intelligence

This week in the data center world:

🆕 {new_facilities} new facilities added to our database
🌍 {top_markets} were the most active markets
⚡ {total_power} MW of new capacity announced

Top Stories:
{top_stories}

Stay ahead with DC Hub → dchub.cloud

#DataCenters #WeeklyDigest #CloudInfrastructure #AI
""",
    
    'power_insight': """⚡ Power & Infrastructure Insight

{insight_title}

{insight_body}

Key Takeaways:
{takeaways}

How is power availability affecting your data center strategy?

#DataCenterPower #EnergyInfrastructure #GridCapacity
""",
    
    'industry_news': """📰 Data Center Industry News

{headline}

{summary}

Impact Analysis:
{analysis}

What's your take on this development?

#DataCenterNews #TechInfrastructure #{topic_tag}
"""
}

def generate_market_update_post(market: str, stats: Dict) -> str:
    """Generate a market update post"""
    highlights = []
    if stats.get('growth_pct'):
        highlights.append(f"📈 {stats['growth_pct']}% YoY growth")
    if stats.get('new_facilities'):
        highlights.append(f"🆕 {stats['new_facilities']} new facilities this quarter")
    if stats.get('total_power'):
        highlights.append(f"⚡ {stats['total_power']} MW total capacity")
    if stats.get('top_provider'):
        highlights.append(f"🏆 {stats['top_provider']} leads the market")
    
    return POST_TEMPLATES['market_update'].format(
        market=market,
        stats=f"• {stats.get('total_facilities', 0)} facilities tracked\n• {stats.get('total_power', 0)} MW capacity",
        highlights='\n'.join(f"• {h}" for h in highlights) if highlights else "• Strong growth continues",
        market_tag=market.replace(' ', '')
    )

def generate_facility_post(facility: Dict) -> str:
    """Generate a new facility announcement post"""
    return POST_TEMPLATES['new_facility'].format(
        provider=facility.get('provider', 'A major operator'),
        location=f"{facility.get('city', '')}, {facility.get('state', '')}",
        power=facility.get('power_mw', 'TBD'),
        sqft=facility.get('sqft', 'TBD'),
        timeline=facility.get('timeline', 'TBD'),
        market=facility.get('market', 'local'),
        total_facilities=facility.get('market_total', '100+'),
        provider_tag=facility.get('provider', 'DataCenter').replace(' ', ''),
        market_tag=facility.get('market', 'DataCenter').replace(' ', '')
    )

def generate_weekly_digest() -> str:
    """Generate weekly digest post"""
    conn = get_db()
    c = conn.cursor()
    
    # Get stats from last 7 days
    week_ago = (datetime.utcnow() - timedelta(days=7)).isoformat()
    
    c.execute("""
        SELECT COUNT(*) FROM facilities 
        WHERE first_seen > ?
    """, [week_ago])
    new_facilities = c.fetchone()[0]
    
    c.execute("""
        SELECT country, COUNT(*) as cnt 
        FROM facilities 
        WHERE first_seen > ?
        GROUP BY country 
        ORDER BY cnt DESC 
        LIMIT 3
    """, [week_ago])
    top_markets = ', '.join([row[0] for row in c.fetchall()]) or 'US, DE, GB'
    
    c.execute("""
        SELECT SUM(power_mw) FROM facilities 
        WHERE first_seen > ? AND power_mw > 0
    """, [week_ago])
    total_power = c.fetchone()[0] or 0
    
    c.execute("""
        SELECT title FROM announcements 
        WHERE discovered_at > ?
        ORDER BY discovered_at DESC
        LIMIT 3
    """, [week_ago])
    stories = c.fetchall()
    
    conn.close()
    
    top_stories = '\n'.join([f"• {s[0][:60]}..." for s in stories]) if stories else "• Hyperscale expansion continues globally"
    
    return POST_TEMPLATES['weekly_digest'].format(
        new_facilities=new_facilities,
        top_markets=top_markets,
        total_power=round(total_power, 1),
        top_stories=top_stories
    )

def generate_post_from_announcement(announcement: Dict) -> str:
    """Generate post from news announcement"""
    return POST_TEMPLATES['industry_news'].format(
        headline=announcement.get('title', 'Breaking News'),
        summary=announcement.get('summary', '')[:200],
        analysis="This continues the trend of infrastructure expansion driven by AI and cloud demand.",
        topic_tag='AIInfrastructure'
    )

# =============================================================================
# API ENDPOINTS
# =============================================================================

@linkedin_bp.route('/api/linkedin/posts', methods=['GET'])
def list_posts():
    """List LinkedIn posts"""
    status = request.args.get('status')
    limit = min(int(request.args.get('limit', 50)), 100)
    
    conn = get_db()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    query = "SELECT * FROM linkedin_posts"
    params = []
    
    if status:
        query += " WHERE status = ?"
        params.append(status)
    
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    
    c.execute(query, params)
    posts = [dict(row) for row in c.fetchall()]
    conn.close()
    
    return jsonify({
        'success': True,
        'posts': posts,
        'count': len(posts)
    })

@linkedin_bp.route('/api/linkedin/posts', methods=['POST'])
def create_post():
    """Create a new LinkedIn post"""
    data = request.get_json() or {}
    
    content = data.get('content')
    post_type = data.get('type', 'custom')
    scheduled_at = data.get('scheduled_at')
    
    if not content:
        return jsonify({'error': 'Content required'}), 400
    
    post_id = hashlib.md5(f"{content[:50]}:{datetime.utcnow().isoformat()}".encode()).hexdigest()[:16]
    now = datetime.utcnow().isoformat()
    
    conn = get_db()
    c = conn.cursor()
    
    c.execute("""
        INSERT INTO linkedin_posts 
        (id, content, post_type, status, scheduled_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, [
        post_id, content, post_type,
        'scheduled' if scheduled_at else 'draft',
        scheduled_at, now
    ])
    
    conn.commit()
    conn.close()
    
    return jsonify({
        'success': True,
        'post_id': post_id,
        'message': 'Post created'
    })

@linkedin_bp.route('/api/linkedin/posts/<post_id>', methods=['DELETE'])
def delete_post(post_id):
    """Delete a post"""
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM linkedin_posts WHERE id = ?", [post_id])
    conn.commit()
    conn.close()
    
    return jsonify({'success': True})

@linkedin_bp.route('/api/linkedin/generate', methods=['POST'])
def generate_post():
    """Generate a post using AI"""
    data = request.get_json() or {}
    post_type = data.get('type', 'weekly_digest')
    params = data.get('params', {})
    
    if post_type == 'weekly_digest':
        content = generate_weekly_digest()
    elif post_type == 'market_update':
        content = generate_market_update_post(
            params.get('market', 'Northern Virginia'),
            params.get('stats', {})
        )
    elif post_type == 'facility':
        content = generate_facility_post(params.get('facility', {}))
    elif post_type == 'news':
        content = generate_post_from_announcement(params.get('announcement', {}))
    else:
        content = "📊 Data Center Market Update\n\nStay tuned for insights!\n\n#DataCenters"
    
    return jsonify({
        'success': True,
        'content': content,
        'type': post_type
    })

@linkedin_bp.route('/api/linkedin/posts/<post_id>/publish', methods=['POST'])
def publish_post(post_id):
    """Publish a post to LinkedIn"""
    if not LINKEDIN_ACCESS_TOKEN:
        return jsonify({
            'error': 'LinkedIn not configured',
            'message': 'Set LINKEDIN_ACCESS_TOKEN environment variable'
        }), 400
    
    conn = get_db()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    c.execute("SELECT content FROM linkedin_posts WHERE id = ?", [post_id])
    row = c.fetchone()
    
    if not row:
        conn.close()
        return jsonify({'error': 'Post not found'}), 404
    
    content = row['content']
    
    # LinkedIn API call
    try:
        # For personal profile
        url = "https://api.linkedin.com/v2/ugcPosts"
        
        headers = {
            'Authorization': f'Bearer {LINKEDIN_ACCESS_TOKEN}',
            'Content-Type': 'application/json',
            'X-Restli-Protocol-Version': '2.0.0'
        }
        
        # Get user URN first
        me_response = requests.get(
            'https://api.linkedin.com/v2/me',
            headers=headers
        )
        
        if not me_response.ok:
            return jsonify({
                'error': 'Failed to get LinkedIn profile',
                'details': me_response.text
            }), 400
        
        user_id = me_response.json().get('id')
        author = f"urn:li:person:{user_id}"
        
        # If posting as company page
        if LINKEDIN_ORG_ID:
            author = f"urn:li:organization:{LINKEDIN_ORG_ID}"
        
        post_data = {
            "author": author,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {
                        "text": content
                    },
                    "shareMediaCategory": "NONE"
                }
            },
            "visibility": {
                "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
            }
        }
        
        response = requests.post(url, headers=headers, json=post_data)
        
        if response.ok:
            linkedin_post_id = response.json().get('id', '')
            
            c.execute("""
                UPDATE linkedin_posts 
                SET status = 'published', posted_at = ?, linkedin_post_id = ?
                WHERE id = ?
            """, [datetime.utcnow().isoformat(), linkedin_post_id, post_id])
            conn.commit()
            conn.close()
            
            return jsonify({
                'success': True,
                'linkedin_post_id': linkedin_post_id,
                'message': 'Posted to LinkedIn!'
            })
        else:
            conn.close()
            return jsonify({
                'error': 'LinkedIn API error',
                'details': response.text
            }), 400
            
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 500

@linkedin_bp.route('/api/linkedin/schedule', methods=['GET'])
def get_schedule():
    """Get posting schedule"""
    conn = get_db()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    c.execute("SELECT * FROM linkedin_schedule ORDER BY day_of_week, hour")
    schedule = [dict(row) for row in c.fetchall()]
    conn.close()
    
    # Default schedule if empty
    if not schedule:
        schedule = [
            {'day_of_week': 1, 'hour': 9, 'enabled': 1},   # Monday 9am
            {'day_of_week': 2, 'hour': 14, 'enabled': 1},  # Tuesday 2pm
            {'day_of_week': 3, 'hour': 9, 'enabled': 1},   # Wednesday 9am
            {'day_of_week': 4, 'hour': 14, 'enabled': 1},  # Thursday 2pm
        ]
    
    return jsonify({
        'success': True,
        'schedule': schedule
    })

@linkedin_bp.route('/api/linkedin/config', methods=['GET'])
def get_config():
    """Get LinkedIn configuration status"""
    return jsonify({
        'success': True,
        'configured': bool(LINKEDIN_ACCESS_TOKEN),
        'client_id_set': bool(LINKEDIN_CLIENT_ID),
        'org_id_set': bool(LINKEDIN_ORG_ID)
    })

# =============================================================================
# REGISTER BLUEPRINT
# =============================================================================

def register_linkedin_poster(app):
    """Register LinkedIn poster blueprint"""
    app.register_blueprint(linkedin_bp)
    print("📱 LinkedIn Poster API registered")

__all__ = ['linkedin_bp', 'register_linkedin_poster', 'generate_weekly_digest']
