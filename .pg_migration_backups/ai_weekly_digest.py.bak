"""
DC Hub — Weekly AI Agent Stats Digest
=======================================
Generates a weekly summary of AI platform activity.
Can be called by cron, email system, or as an API endpoint.

Usage in main.py:
  from ai_weekly_digest import register_digest_routes
  register_digest_routes(app)

Endpoints:
  GET /api/ai/weekly-digest          → JSON digest data
  GET /api/ai/weekly-digest?format=html  → HTML email-ready report
"""

import os
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger('ai_digest')


def _get_conn():
    """Get Neon connection."""
    import psycopg2
    db_url = os.environ.get('DATABASE_URL', '') or os.environ.get('NEON_DATABASE_URL', '')
    return psycopg2.connect(db_url, connect_timeout=10)


def _query(sql, params=None):
    """Execute query and return dict rows."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description] if cur.description else []
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.commit()
    conn.close()
    return rows


def generate_weekly_digest():
    """Generate the weekly AI agent stats digest as a dict."""
    now = datetime.now(timezone.utc)
    week_start = (now - timedelta(days=7)).strftime('%Y-%m-%d')
    prev_week_start = (now - timedelta(days=14)).strftime('%Y-%m-%d')

    NON_AI = ('direct', 'seo_bot', 'media_crawler', 'unknown_ai', 'mcp-remote-fallback-test', 'test')

    # This week's stats
    this_week = _query("""
        SELECT platform, SUM(request_count) as requests
        FROM ai_daily_stats
        WHERE date::date >= %s::date
        GROUP BY platform
        ORDER BY requests DESC
    """, (week_start,))

    # Previous week's stats (for comparison)
    prev_week = _query("""
        SELECT platform, SUM(request_count) as requests
        FROM ai_daily_stats
        WHERE date::date >= %s::date AND date::date < %s::date
        GROUP BY platform
    """, (prev_week_start, week_start))
    prev_map = {r['platform']: r['requests'] for r in prev_week}

    # Cumulative totals
    cumulative = _query("""
        SELECT platform, total_requests, last_seen::text
        FROM ai_cumulative
        ORDER BY total_requests DESC
    """)

    # Build digest
    ai_this_week = [r for r in this_week if r['platform'] not in NON_AI]
    ai_prev_total = sum(prev_map.get(r['platform'], 0) for r in ai_this_week)
    ai_this_total = sum(r['requests'] for r in ai_this_week)

    total_all_time = sum(r.get('total_requests', 0) for r in cumulative)
    ai_all_time = sum(r.get('total_requests', 0) for r in cumulative if r['platform'] not in NON_AI)

    # Week-over-week change
    if ai_prev_total > 0:
        wow_change = ((ai_this_total - ai_prev_total) / ai_prev_total) * 100
    elif ai_this_total > 0:
        wow_change = 100.0
    else:
        wow_change = 0.0

    # Platform details
    platform_details = []
    for r in ai_this_week:
        prev = prev_map.get(r['platform'], 0)
        change = ((r['requests'] - prev) / prev * 100) if prev > 0 else (100 if r['requests'] > 0 else 0)
        cum = next((c for c in cumulative if c['platform'] == r['platform']), {})
        platform_details.append({
            'platform': r['platform'],
            'this_week': r['requests'],
            'prev_week': prev,
            'change_pct': round(change, 1),
            'all_time': cum.get('total_requests', 0),
            'last_seen': cum.get('last_seen', ''),
        })

    # New platforms this week (seen this week but 0 prev week)
    new_platforms = [p for p in platform_details if p['prev_week'] == 0 and p['this_week'] > 0]

    # MCP specific
    mcp_this = next((r['requests'] for r in this_week if r['platform'] == 'mcp'), 0)
    mcp_prev = prev_map.get('mcp', 0)

    digest = {
        'period': f"{week_start} to {now.strftime('%Y-%m-%d')}",
        'generated_at': now.isoformat(),
        'summary': {
            'ai_requests_this_week': ai_this_total,
            'ai_requests_prev_week': ai_prev_total,
            'wow_change_pct': round(wow_change, 1),
            'total_all_time': total_all_time,
            'ai_all_time': ai_all_time,
            'active_ai_platforms': len(ai_this_week),
            'mcp_requests': mcp_this,
            'mcp_prev_week': mcp_prev,
        },
        'platforms': platform_details,
        'new_platforms': new_platforms,
        'highlights': [],
    }

    # Generate highlights
    if wow_change > 20:
        digest['highlights'].append(f"AI platform traffic up {wow_change:.0f}% week-over-week")
    if mcp_this > mcp_prev * 1.5 and mcp_prev > 0:
        digest['highlights'].append(f"MCP traffic grew {((mcp_this-mcp_prev)/mcp_prev*100):.0f}% ({mcp_prev} → {mcp_this})")
    if new_platforms:
        names = ', '.join(p['platform'] for p in new_platforms)
        digest['highlights'].append(f"New AI platforms this week: {names}")

    top_grower = max(platform_details, key=lambda p: p['change_pct']) if platform_details else None
    if top_grower and top_grower['change_pct'] > 50 and top_grower['prev_week'] > 0:
        digest['highlights'].append(f"Fastest growing: {top_grower['platform']} (+{top_grower['change_pct']:.0f}%)")

    return digest


def digest_to_html(digest):
    """Convert digest dict to HTML email body."""
    s = digest['summary']
    wow_arrow = '↑' if s['wow_change_pct'] > 0 else '↓' if s['wow_change_pct'] < 0 else '→'
    wow_color = '#00ff88' if s['wow_change_pct'] > 0 else '#ff4444' if s['wow_change_pct'] < 0 else '#888'

    html = f"""
    <div style="font-family:Arial,sans-serif; max-width:600px; margin:0 auto; background:#0a0a0a; color:#ffffff; padding:32px; border-radius:12px;">
      <div style="text-align:center; margin-bottom:24px;">
        <div style="font-size:24px; font-weight:700; color:#00ff88;">DC Hub</div>
        <div style="font-size:14px; color:#a0a0a0;">Weekly AI Agent Intelligence Report</div>
        <div style="font-size:11px; color:#666; margin-top:4px;">{digest['period']}</div>
      </div>

      <!-- Summary Stats -->
      <div style="display:flex; gap:12px; margin-bottom:24px;">
        <div style="flex:1; background:#111; border:1px solid #2a2a2a; border-radius:8px; padding:16px; text-align:center;">
          <div style="font-size:10px; color:#a0a0a0; text-transform:uppercase; letter-spacing:1px;">This Week</div>
          <div style="font-size:28px; font-weight:700; color:#00ff88;">{s['ai_requests_this_week']:,}</div>
          <div style="font-size:11px; color:{wow_color};">{wow_arrow} {abs(s['wow_change_pct']):.0f}% vs last week</div>
        </div>
        <div style="flex:1; background:#111; border:1px solid #2a2a2a; border-radius:8px; padding:16px; text-align:center;">
          <div style="font-size:10px; color:#a0a0a0; text-transform:uppercase; letter-spacing:1px;">All Time</div>
          <div style="font-size:28px; font-weight:700; color:#8b5cf6;">{s['ai_all_time']:,}</div>
          <div style="font-size:11px; color:#a0a0a0;">{s['active_ai_platforms']} platforms active</div>
        </div>
        <div style="flex:1; background:#111; border:1px solid #2a2a2a; border-radius:8px; padding:16px; text-align:center;">
          <div style="font-size:10px; color:#a0a0a0; text-transform:uppercase; letter-spacing:1px;">MCP Traffic</div>
          <div style="font-size:28px; font-weight:700; color:#4488ff;">{s['mcp_requests']:,}</div>
          <div style="font-size:11px; color:#a0a0a0;">developer integrations</div>
        </div>
      </div>
    """

    # Highlights
    if digest['highlights']:
        html += '<div style="background:#111; border-left:3px solid #00ff88; border-radius:0 8px 8px 0; padding:14px 18px; margin-bottom:24px;">'
        html += '<div style="font-size:12px; font-weight:700; color:#00ff88; margin-bottom:8px;">HIGHLIGHTS</div>'
        for h in digest['highlights']:
            html += f'<div style="font-size:13px; color:#ccc; margin-bottom:4px;">→ {h}</div>'
        html += '</div>'

    # Platform table
    html += """
      <div style="margin-bottom:24px;">
        <div style="font-size:12px; font-weight:700; color:#a0a0a0; text-transform:uppercase; letter-spacing:1px; margin-bottom:12px;">Platform Breakdown</div>
        <table style="width:100%; border-collapse:collapse; font-size:13px;">
          <tr style="border-bottom:1px solid #2a2a2a;">
            <th style="text-align:left; padding:8px 0; color:#666; font-size:10px; text-transform:uppercase;">Platform</th>
            <th style="text-align:right; padding:8px 0; color:#666; font-size:10px;">This Week</th>
            <th style="text-align:right; padding:8px 0; color:#666; font-size:10px;">Prev Week</th>
            <th style="text-align:right; padding:8px 0; color:#666; font-size:10px;">Change</th>
          </tr>
    """

    for p in digest['platforms']:
        change_color = '#00ff88' if p['change_pct'] > 0 else '#ff4444' if p['change_pct'] < 0 else '#666'
        change_str = f"+{p['change_pct']:.0f}%" if p['change_pct'] > 0 else f"{p['change_pct']:.0f}%"
        if p['prev_week'] == 0 and p['this_week'] > 0:
            change_str = 'NEW'
            change_color = '#00ff88'
        html += f"""
          <tr style="border-bottom:1px solid #1a1a1a;">
            <td style="padding:8px 0; font-weight:600;">{p['platform']}</td>
            <td style="text-align:right; padding:8px 0; font-family:monospace;">{p['this_week']:,}</td>
            <td style="text-align:right; padding:8px 0; color:#666; font-family:monospace;">{p['prev_week']:,}</td>
            <td style="text-align:right; padding:8px 0; color:{change_color}; font-weight:600;">{change_str}</td>
          </tr>
        """

    html += """
        </table>
      </div>

      <div style="text-align:center; padding-top:16px; border-top:1px solid #2a2a2a;">
        <a href="https://dchub.cloud/ai-analytics" style="display:inline-block; background:#00ff8818; color:#00ff88; border:1px solid #00ff8844; border-radius:6px; padding:10px 24px; text-decoration:none; font-size:13px; font-weight:600;">View Live Dashboard →</a>
        <div style="font-size:10px; color:#666; margin-top:12px;">DC Hub — Data Center Intelligence for the AI Era</div>
      </div>
    </div>
    """

    return html


def register_digest_routes(app):
    """Register the weekly digest API endpoint."""
    from flask import request, make_response, jsonify

    @app.route('/api/ai/weekly-digest', methods=['GET', 'OPTIONS'])
    def weekly_digest_endpoint():
        if request.method == 'OPTIONS':
            resp = make_response('', 200)
            resp.headers['Access-Control-Allow-Origin'] = '*'
            return resp

        try:
            digest = generate_weekly_digest()
            fmt = request.args.get('format', 'json')

            if fmt == 'html':
                html = digest_to_html(digest)
                resp = make_response(html, 200)
                resp.headers['Content-Type'] = 'text/html'
                resp.headers['Access-Control-Allow-Origin'] = '*'
                return resp
            else:
                resp = make_response(jsonify(digest), 200)
                resp.headers['Access-Control-Allow-Origin'] = '*'
                return resp

        except Exception as e:
            logger.error(f"[AI Digest] Error: {e}")
            return jsonify({'error': str(e)}), 500

    logger.info("[AI Digest] ✅ Weekly digest endpoint registered: /api/ai/weekly-digest")
