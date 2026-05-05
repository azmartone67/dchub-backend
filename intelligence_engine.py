"""
DC Hub Intelligence Engine - Daily Email, LinkedIn, Deal Alerts
================================================================
Automated intelligence system for DC Hub Nexus.

Features:
- Daily Intelligence Email summaries
- LinkedIn Auto-Posting with AI content
- Deal/Capacity Alerts (webhook-based)
- Market trend notifications
"""

import os
import json
import hashlib
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from flask import Blueprint, request, jsonify
from db_utils import get_db

DB_PATH = os.environ.get('DB_PATH', 'dc_nexus.db')

LINKEDIN_ACCESS_TOKEN = os.environ.get('LINKEDIN_ACCESS_TOKEN', '')
LINKEDIN_PERSON_ID = os.environ.get('LINKEDIN_PERSON_ID', '')
LINKEDIN_ORG_ID = os.environ.get('LINKEDIN_ORG_ID', '')  # Company page ID for posting as DC Hub

intelligence_bp = Blueprint('intelligence', __name__)

def init_intelligence_db():
    """Initialize intelligence tables"""
    conn = get_db()
    try:
        c = conn.cursor()

        c.execute("""
            CREATE TABLE IF NOT EXISTS intelligence_emails (
                id TEXT PRIMARY KEY,
                email_type TEXT,
                subject TEXT,
                content TEXT,
                sent_at TEXT,
                status TEXT DEFAULT 'pending'
            )
        """)

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
            CREATE TABLE IF NOT EXISTS deal_alerts (
                id TEXT PRIMARY KEY,
                alert_type TEXT,
                title TEXT,
                details TEXT,
                companies TEXT,
                capacity_mw REAL,
                location TEXT,
                detected_at TEXT,
                notified INTEGER DEFAULT 0
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS alert_subscriptions (
                id TEXT PRIMARY KEY,
                webhook_url TEXT,
                alert_types TEXT,
                markets TEXT,
                companies TEXT,
                created_at TEXT,
                active INTEGER DEFAULT 1
            )
        """)

        conn.commit()
    finally:
        conn.close()

init_intelligence_db()


def get_last_linkedin_post_time() -> Optional[datetime]:
    """Get the last LinkedIn post time from database"""
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("SELECT posted_at FROM linkedin_posts WHERE status = 'posted' ORDER BY posted_at DESC LIMIT 1")
        result = c.fetchone()
        if result and result[0]:
            return datetime.fromisoformat(result[0])
    except:
        pass
    finally:
        conn.close()
    return None


def save_linkedin_post_time(posted_at: datetime) -> None:
    """Save LinkedIn post time to database (for persistence across restarts)"""
    pass


def get_daily_stats() -> Dict:
    """Get today's statistics for intelligence reports"""
    conn = get_db()
    try:
        # sqlite3.Row removed - PostgreSQL uses RealDictCursor or dict(row)
        c = conn.cursor()

        today = datetime.now().strftime('%Y-%m-%d')
        yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')

        stats = {
            'date': today,
            'facilities': {'total': 0, 'new_today': 0},
            'capacity': {'total_mw': 0, 'new_projects': 0, 'pipeline_mw': 0},
            'news': {'total': 0, 'new_today': 0},
            'top_markets': [],
            'top_operators': [],
            'recent_deals': [],
            'capacity_projects': []
        }

        c.execute("SELECT COUNT(*) FROM facilities")
        stats['facilities']['total'] = c.fetchone()[0]

        c.execute(f"SELECT COUNT(*) FROM facilities WHERE first_seen LIKE '{today}%'")
        stats['facilities']['new_today'] = c.fetchone()[0]

        c.execute("SELECT SUM(power_mw) FROM facilities WHERE power_mw > 0")
        result = c.fetchone()[0]
        stats['capacity']['total_mw'] = round(result, 1) if result else 0

        c.execute("SELECT COUNT(*) FROM announcements")
        stats['news']['total'] = c.fetchone()[0]

        c.execute(f"SELECT COUNT(*) FROM announcements WHERE discovered_at LIKE '{today}%'")
        stats['news']['new_today'] = c.fetchone()[0]

        c.execute("""
            SELECT city, COUNT(*) as cnt FROM facilities
            WHERE city IS NOT NULL AND city != ''
            GROUP BY city ORDER BY cnt DESC LIMIT 10
        """)
        stats['top_markets'] = [{'market': row['city'], 'count': row['cnt']} for row in c.fetchall()]

        c.execute("""
            SELECT provider, COUNT(*) as cnt FROM facilities
            WHERE provider IS NOT NULL AND provider != ''
            GROUP BY provider ORDER BY cnt DESC LIMIT 10
        """)
        stats['top_operators'] = [{'operator': row['provider'], 'count': row['cnt']} for row in c.fetchall()]

        try:
            c.execute("""
                SELECT * FROM capacity_tracking
                WHERE discovered_at LIKE %s
                AND operator IS NOT NULL AND operator != 'Company'
                ORDER BY capacity_mw DESC LIMIT 10
            """, (f'{today}%',))
            stats['capacity_projects'] = [dict(row) for row in c.fetchall()]

            # Map 'operator' to 'company' for compatibility
            for p in stats['capacity_projects']:
                p['company'] = p.get('operator', 'Unknown')

            c.execute("SELECT SUM(capacity_mw) FROM capacity_tracking")
            result = c.fetchone()[0]
            stats['capacity']['pipeline_mw'] = round(result, 1) if result else 0
        except:
            pass

        try:
            c.execute("""
                SELECT * FROM deals
                WHERE date LIKE %s
                ORDER BY date DESC LIMIT 5
            """, (f'{today}%',))
            stats['recent_deals'] = [dict(row) for row in c.fetchall()]
        except:
            pass

    finally:
        conn.close()
    return stats


def generate_daily_email_content() -> Tuple[str, str, str]:
    """Generate daily intelligence email content"""
    stats = get_daily_stats()
    
    subject = f"DC Hub Daily Intel - {stats['date']} | {stats['news']['new_today']} News, {stats['capacity']['pipeline_mw']:,.0f} MW Pipeline"
    
    html_content = f"""
<!DOCTYPE html>
<html>
<head>
    <style>
        body {{ font-family: 'Segoe UI', Tahoma, sans-serif; background: #1a1a2e; color: #eaeaea; padding: 20px; }}
        .container {{ max-width: 600px; margin: 0 auto; background: #16213e; border-radius: 12px; padding: 30px; }}
        .header {{ text-align: center; border-bottom: 2px solid #0f3460; padding-bottom: 20px; margin-bottom: 20px; }}
        .header h1 {{ color: #00d9ff; margin: 0; font-size: 24px; }}
        .header p {{ color: #888; margin: 5px 0 0; }}
        .stats-grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 15px; margin: 20px 0; }}
        .stat-card {{ background: #0f3460; border-radius: 8px; padding: 15px; text-align: center; }}
        .stat-value {{ font-size: 28px; font-weight: bold; color: #00d9ff; }}
        .stat-label {{ color: #888; font-size: 12px; text-transform: uppercase; }}
        .section {{ margin: 25px 0; }}
        .section h2 {{ color: #00d9ff; font-size: 16px; border-bottom: 1px solid #0f3460; padding-bottom: 8px; }}
        .list-item {{ background: #0f3460; border-radius: 6px; padding: 12px; margin: 8px 0; }}
        .highlight {{ color: #00ff88; font-weight: bold; }}
        .footer {{ text-align: center; margin-top: 30px; padding-top: 20px; border-top: 1px solid #0f3460; color: #666; font-size: 12px; }}
        a {{ color: #00d9ff; text-decoration: none; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>DC Hub Daily Intelligence</h1>
            <p>{stats['date']}</p>
        </div>
        
        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-value">{stats['facilities']['total']:,}</div>
                <div class="stat-label">Total Facilities</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{stats['news']['new_today']}</div>
                <div class="stat-label">New Articles Today</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{stats['capacity']['pipeline_mw']:,.0f}</div>
                <div class="stat-label">MW in Pipeline</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{len(stats['capacity_projects'])}</div>
                <div class="stat-label">New Projects Today</div>
            </div>
        </div>
        
        <div class="section">
            <h2>Top Markets by Facility Count</h2>
            {''.join([f'<div class="list-item">{m["market"]}: <span class="highlight">{m["count"]}</span> facilities</div>' for m in stats['top_markets'][:5]])}
        </div>
        
        <div class="section">
            <h2>Top Operators</h2>
            {''.join([f'<div class="list-item">{o["operator"]}: <span class="highlight">{o["count"]}</span> sites</div>' for o in stats['top_operators'][:5]])}
        </div>
        
        {"<div class='section'><h2>Today's Capacity Projects</h2>" + ''.join([f'<div class="list-item"><strong>{p.get("company", "Unknown")}</strong>: {p.get("capacity_mw", 0):.0f} MW in {p.get("location", "Unknown")}</div>' for p in stats['capacity_projects'][:5]]) + "</div>" if stats['capacity_projects'] else ""}
        
        <div class="footer">
            <p>Powered by DC Hub Nexus | <a href="https://dchub.cloud">dchub.cloud</a></p>
            <p>Data Center Intelligence Platform</p>
        </div>
    </div>
</body>
</html>
"""
    
    text_content = f"""
DC Hub Daily Intelligence - {stats['date']}
============================================

SUMMARY
- Total Facilities: {stats['facilities']['total']:,}
- New Articles Today: {stats['news']['new_today']}
- MW in Pipeline: {stats['capacity']['pipeline_mw']:,.0f}
- New Projects Today: {len(stats['capacity_projects'])}

TOP MARKETS
{chr(10).join([f"- {m['market']}: {m['count']} facilities" for m in stats['top_markets'][:5]])}

TOP OPERATORS
{chr(10).join([f"- {o['operator']}: {o['count']} sites" for o in stats['top_operators'][:5]])}

---
Powered by DC Hub Nexus | dchub.cloud
"""
    
    return subject, html_content, text_content


def generate_linkedin_post() -> str:
    """Generate an AI-powered LinkedIn post from today's data"""
    stats = get_daily_stats()
    
    top_project = stats['capacity_projects'][0] if stats['capacity_projects'] else None
    
    if top_project and top_project.get('company') and top_project.get('company') != 'Unknown':
        location = top_project.get('location') or 'a key market'
        post = f"""Data Center Market Update

Today's highlight: {top_project.get('company', 'Major operator')} announced {top_project.get('capacity_mw', 0):.0f} MW of new capacity in {location}.

Market Pulse:
- {stats['news']['new_today']} industry articles published today
- {stats['capacity']['pipeline_mw']:,.0f} MW total capacity in development pipeline
- {stats['facilities']['total']:,} data center facilities tracked globally

Top Active Markets: {', '.join([m['market'] for m in stats['top_markets'][:3]])}

The data center industry continues to see unprecedented growth driven by AI and cloud demand.

#DataCenters #AI #CloudInfrastructure #MarketIntelligence #DCHub"""
    else:
        post = f"""Daily Data Center Intelligence

Market snapshot for {stats['date']}:

- {stats['facilities']['total']:,} facilities tracked worldwide
- {stats['news']['new_today']} new industry articles today
- {stats['capacity']['pipeline_mw']:,.0f} MW in development pipeline

Leading operators: {', '.join([o['operator'] for o in stats['top_operators'][:3]])}
Key markets: {', '.join([m['market'] for m in stats['top_markets'][:3]])}

Stay informed with DC Hub - your source for data center intelligence.

#DataCenters #Infrastructure #CloudComputing #MarketAnalysis"""
    
    return post


def post_to_linkedin(content: str) -> Dict:
    """Post content to LinkedIn using the API - posts as DC Hub company page if configured"""
    if not LINKEDIN_ACCESS_TOKEN:
        return {'success': False, 'error': 'No LinkedIn access token configured'}
    
    try:
        # Post as company page if LINKEDIN_ORG_ID is configured, otherwise personal profile
        if LINKEDIN_ORG_ID:
            author_urn = f"urn:li:organization:{LINKEDIN_ORG_ID}"
            print(f"📱 Posting to LinkedIn as DC Hub company page (org: {LINKEDIN_ORG_ID})")
        else:
            me_response = requests.get(
                'https://api.linkedin.com/v2/userinfo',
                headers={'Authorization': f'Bearer {LINKEDIN_ACCESS_TOKEN}'}
            )
            
            if me_response.status_code != 200:
                return {'success': False, 'error': f'Failed to get user info: {me_response.text}'}
            
            user_info = me_response.json()
            author_urn = f"urn:li:person:{user_info.get('sub', '')}"
            print(f"📱 Posting to LinkedIn as personal profile")
        
        # Use LinkedIn's newer Posts API (v202401)
        post_data = {
            "author": author_urn,
            "commentary": content,
            "visibility": "PUBLIC",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": []
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False
        }
        
        response = requests.post(
            'https://api.linkedin.com/rest/posts',
            headers={
                'Authorization': f'Bearer {LINKEDIN_ACCESS_TOKEN}',
                'Content-Type': 'application/json',
                'X-Restli-Protocol-Version': '2.0.0',
                'LinkedIn-Version': '202601'
            },
            json=post_data
        )
        
        if response.status_code in [200, 201]:
            post_id = response.headers.get('x-restli-id', response.json().get('id', 'unknown'))
            
            conn = get_db()
            try:
                c = conn.cursor()
                c.execute("""
                    INSERT INTO linkedin_posts  (id, content, post_type, status, posted_at, linkedin_post_id, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (
                    hashlib.md5(content.encode()).hexdigest()[:16],
                    content,
                    'daily_update',
                    'posted',
                    datetime.now().isoformat(),
                    post_id,
                    datetime.now().isoformat()
                ))
                conn.commit()
            finally:
                conn.close()
            
            return {'success': True, 'post_id': post_id}
        else:
            return {'success': False, 'error': response.text, 'status_code': response.status_code}
            
    except Exception as e:
        return {'success': False, 'error': str(e)}


def check_for_new_deals() -> List[Dict]:
    """Check for new M&A deals and capacity announcements to alert on"""
    conn = get_db()
    try:
        # sqlite3.Row removed - PostgreSQL uses RealDictCursor or dict(row)
        c = conn.cursor()

        alerts = []
        today = datetime.now().strftime('%Y-%m-%d')

        try:
            c.execute("""
                SELECT * FROM capacity_tracking
                WHERE discovered_at LIKE %s AND capacity_mw >= 100
                ORDER BY capacity_mw DESC LIMIT 10
            """, (f'{today}%',))

            for row in c.fetchall():
                alert = {
                    'type': 'capacity',
                    'title': f"Major Capacity: {row['company']} - {row['capacity_mw']:.0f} MW",
                    'details': f"{row['company']} announced {row['capacity_mw']:.0f} MW in {row['location']}",
                    'company': row['company'],
                    'capacity_mw': row['capacity_mw'],
                    'location': row['location'],
                    'detected_at': row['discovered_at']
                }
                alerts.append(alert)
        except:
            pass

        try:
            c.execute("""
                SELECT * FROM announcements
                WHERE discovered_at LIKE %s
                AND (LOWER(title) LIKE '%acqui%' OR LOWER(title) LIKE '%merger%'
                     OR LOWER(title) LIKE '%billion%' OR LOWER(title) LIKE '%deal%')
                ORDER BY published_date DESC LIMIT 10
            """, (f'{today}%',))

            for row in c.fetchall():
                alert = {
                    'type': 'deal',
                    'title': row['title'][:100],
                    'details': row['summary'][:200] if row['summary'] else '',
                    'source': row['source'],
                    'detected_at': row['discovered_at']
                }
                alerts.append(alert)
        except:
            pass

    finally:
        conn.close()
    return alerts


def send_deal_alerts(alerts: List[Dict]) -> Dict:
    """Send alerts to all subscribed webhooks"""
    if not alerts:
        return {'sent': 0, 'errors': []}
    
    conn = get_db()
    try:
        # sqlite3.Row removed - PostgreSQL uses RealDictCursor or dict(row)
        c = conn.cursor()

        c.execute("SELECT * FROM alert_subscriptions WHERE active = 1")
        subscriptions = c.fetchall()

        sent_count = 0
        errors = []

        for sub in subscriptions:
            try:
                payload = {
                    'timestamp': datetime.now().isoformat(),
                    'alerts': alerts,
                    'source': 'DC Hub Nexus'
                }

                response = requests.post(
                    sub['webhook_url'],
                    json=payload,
                    headers={'Content-Type': 'application/json'},
                    timeout=10
                )

                if response.status_code in [200, 201, 202]:
                    sent_count += 1
                else:
                    errors.append(f"Webhook {sub['id']}: {response.status_code}")
            except Exception as e:
                errors.append(f"Webhook {sub['id']}: {str(e)}")

    finally:
        conn.close()
    return {'sent': sent_count, 'total_alerts': len(alerts), 'errors': errors}


def run_daily_intelligence():
    """Main function to run all daily intelligence tasks"""
    results = {
        'timestamp': datetime.now().isoformat(),
        'email': None,
        'linkedin': None,
        'alerts': None
    }
    
    try:
        subject, html, text = generate_daily_email_content()
        results['email'] = {
            'subject': subject,
            'generated': True,
            'content_length': len(html)
        }
    except Exception as e:
        results['email'] = {'error': str(e)}
    
    try:
        post_content = generate_linkedin_post()
        linkedin_result = post_to_linkedin(post_content)
        results['linkedin'] = linkedin_result
    except Exception as e:
        results['linkedin'] = {'error': str(e)}
    
    try:
        alerts = check_for_new_deals()
        if alerts:
            alert_result = send_deal_alerts(alerts)
            results['alerts'] = alert_result
        else:
            results['alerts'] = {'message': 'No new significant deals detected'}
    except Exception as e:
        results['alerts'] = {'error': str(e)}
    
    return results


@intelligence_bp.route('/api/v1/intelligence/daily', methods=['GET', 'POST'])
def api_daily_intelligence():
    """Trigger or get daily intelligence"""
    if request.method == 'POST':
        results = run_daily_intelligence()
        return jsonify(results)
    else:
        stats = get_daily_stats()
        return jsonify(stats)


@intelligence_bp.route('/api/v1/intelligence/email-preview', methods=['GET'])
def api_email_preview():
    """Preview the daily email content"""
    subject, html, text = generate_daily_email_content()
    return html, 200, {'Content-Type': 'text/html'}


@intelligence_bp.route('/api/v1/intelligence/linkedin-preview', methods=['GET'])
def api_linkedin_preview():
    """Preview the LinkedIn post content"""
    post = generate_linkedin_post()
    return jsonify({'content': post, 'character_count': len(post)})


@intelligence_bp.route('/api/v1/intelligence/linkedin-post', methods=['POST'])
def api_linkedin_post():
    """Post to LinkedIn now"""
    content = request.json.get('content') if request.json else None
    if not content:
        content = generate_linkedin_post()
    
    result = post_to_linkedin(content)
    return jsonify(result)


@intelligence_bp.route('/api/v1/intelligence/alerts', methods=['GET'])
def api_get_alerts():
    """Get recent deal/capacity alerts"""
    alerts = check_for_new_deals()
    return jsonify({'alerts': alerts, 'count': len(alerts)})


@intelligence_bp.route('/api/v1/intelligence/subscribe', methods=['POST'])
def api_subscribe_alerts():
    """Subscribe to deal/capacity alerts"""
    data = request.json
    if not data or not data.get('webhook_url'):
        return jsonify({'error': 'webhook_url required'}), 400
    
    conn = get_db()
    try:
        c = conn.cursor()

        sub_id = hashlib.md5(data['webhook_url'].encode()).hexdigest()[:16]

        c.execute("""
            INSERT INTO alert_subscriptions  (id, webhook_url, alert_types, markets, companies, created_at, active)
            VALUES (%s, %s, %s, %s, %s, %s, 1)
        """, (
            sub_id,
            data['webhook_url'],
            json.dumps(data.get('alert_types', ['deal', 'capacity'])),
            json.dumps(data.get('markets', [])),
            json.dumps(data.get('companies', [])),
            datetime.now().isoformat()
        ))

        conn.commit()
    finally:
        conn.close()
    
    return jsonify({'success': True, 'subscription_id': sub_id})


@intelligence_bp.route('/api/v1/intelligence/stats', methods=['GET'])
def api_intelligence_stats():
    """Get intelligence system statistics"""
    conn = get_db()
    try:
        c = conn.cursor()

        stats = {}

        c.execute("SELECT COUNT(*) FROM linkedin_posts WHERE status = 'posted'")
        stats['linkedin_posts'] = c.fetchone()[0]

        c.execute("SELECT COUNT(*) FROM alert_subscriptions WHERE active = 1")
        stats['active_subscriptions'] = c.fetchone()[0]

        c.execute("SELECT COUNT(*) FROM deal_alerts")
        stats['total_alerts'] = c.fetchone()[0]

    finally:
        conn.close()
    
    return jsonify(stats)


if __name__ == '__main__':
    print("DC Hub Intelligence Engine")
    print("=" * 40)
    
    print("\nGenerating daily stats...")
    stats = get_daily_stats()
    print(f"Facilities: {stats['facilities']['total']:,}")
    print(f"News today: {stats['news']['new_today']}")
    print(f"Pipeline: {stats['capacity']['pipeline_mw']:,.0f} MW")
    
    print("\nGenerating LinkedIn post preview...")
    post = generate_linkedin_post()
    print(post[:300] + "...")
    
    print("\nChecking for alerts...")
    alerts = check_for_new_deals()
    print(f"Found {len(alerts)} potential alerts")
