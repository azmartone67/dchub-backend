"""
DC Hub AI Agent - Complete Working Version
All features: Dashboard, News, Reports, LinkedIn, Chat
"""

from flask import Blueprint, jsonify, request, render_template_string
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
import feedparser
import json
import os
from db_utils import get_db

# Try to import anthropic
try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False
    print("⚠️ anthropic not installed. Run: pip install anthropic")

# Create Blueprint
ai_agent = Blueprint('ai_agent', __name__, url_prefix='/api/agent')

# Configuration
def get_live_dchub_config():
    """Get live stats from database instead of hardcoded values"""
    db_path = os.environ.get('DB_PATH', 'dc_nexus.db')
    
    try:
        conn = get_db(db_path)
        c = conn.cursor()
        
        # Get live facility count
        c.execute("SELECT COUNT(*) FROM facilities")
        facilities_count = c.fetchone()[0] or 0
        
        # Get unique markets count
        c.execute("SELECT COUNT(DISTINCT city) FROM facilities WHERE city IS NOT NULL AND city != ''")
        markets_count = c.fetchone()[0] or None
        
        # Get pipeline MW from capacity_tracking.
        # ★2026-08-19: the `or 13.0` fallbacks here were INVENTED, not measured.
        #  They also disagreed by ~28x with canonical_stats.pipeline_gw = 369,
        #  which is the pinned SoT. An unmeasured number must be None so a
        #  consumer can omit it — never a plausible-looking constant.
        try:
            c.execute("SELECT SUM(capacity_mw) FROM capacity_tracking")
            pipeline_mw = c.fetchone()[0] or 0
            pipeline_gw = round(pipeline_mw / 1000, 1) if pipeline_mw else None
        except Exception:
            pipeline_gw = None
        
        # Get deal volume from transactions
        try:
            c.execute("SELECT COUNT(*) FROM deals")
            total_deals = c.fetchone()[0] or 0
            deal_volume = f"{total_deals} deals tracked"
        except Exception:
            deal_volume = None
        
        conn.close()
        
        return {
            "version": "v88",
            "facilities_count": facilities_count,
            "markets_count": markets_count,
            "pipeline_gw": pipeline_gw,
            "vacancy_rate": 1.6,
            "deal_volume": deal_volume,
            "avg_pricing": "$200+/kW"
        }
    except Exception as e:
        print(f"Error getting live config: {e}")
        # ★★★2026-08-19: this fallback FABRICATED every field, and one of them
        #  was actively harmful: facilities_count 21000 is the PRE-DEDUP figure
        #  that ai_surface_canon.stale_markers lists verbatim as a "~1.7x
        #  over-claim ... scrub on sight". This function reaches the fallback
        #  whenever the DB read raises — and it calls get_db(db_path) with a
        #  SQLite path ('dc_nexus.db', Replit-era) that no longer exists, so the
        #  fabricated branch is the LIKELY path, not the rare one.
        #  Now: derive what canon owns, and publish None for what nothing
        #  measures. A consumer can omit a None; it cannot un-quote a fake.
        try:
            from ai_surface_canon import PINNED as _P
            _fac = _P.get("facilities")
            _mkt = _P.get("markets")
            _deal = _P.get("deals")
        except Exception:
            _fac = _mkt = _deal = None
        return {
            "version": "v88",
            "facilities_count": _fac,          # canon floor phrase, e.g. "18,400+"
            "markets_count": _mkt,             # canon floor phrase, e.g. "300+"
            "pipeline_gw": None,               # UNMEASURED here — see canonical_stats
            "vacancy_rate": None,
            "deal_volume": _deal,
            "avg_pricing": None,
            "_basis": "canon floors + None for unmeasured; no invented constants",
        }

# Use live config
# ★2026-08-19: DCHUB_CONFIG values may now be an int (measured), a canon FLOOR
#  PHRASE ("18,400+"), or None (nothing measures it). The old call sites used
#  `:,` formatting, which raises TypeError on both of the latter — so removing
#  the invented constants without this would have traded a fake number for a
#  500. _fmt renders each honestly and omits what is unknown.
def _fmt(v, unit=""):
    """int -> '18,400'; str -> as-is (already a canon floor); None -> 'not published'."""
    if v is None:
        return "not published"
    if isinstance(v, (int, float)):
        try:
            return f"{v:,}{unit}"
        except Exception:
            return f"{v}{unit}"
    return f"{v}{unit}"


DCHUB_CONFIG = get_live_dchub_config()

NEWS_SOURCES = [
    {"name": "Data Center Dynamics", "url": "https://www.datacenterdynamics.com/en/rss/", "category": "industry"},
    {"name": "Data Center Knowledge", "url": "https://www.datacenterknowledge.com/rss.xml", "category": "industry"},
    {"name": "Data Center Frontier", "url": "https://www.datacenterfrontier.com/feed", "category": "industry"},
    {"name": "The Register", "url": "https://www.theregister.com/data_centre/headlines.atom", "category": "tech"},
    {"name": "TechCrunch", "url": "https://techcrunch.com/tag/data-centers/feed/", "category": "tech"},
]

# Data cache
agent_cache = {
    "news": [],
    "reports": [],
    "market_analysis": {},
    "last_updates": {},
}

# Get Claude client
def get_claude_client():
    if not ANTHROPIC_AVAILABLE:
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        return anthropic.Anthropic(api_key=api_key)
    return None

# Generate market analysis
def generate_market_analysis():
    client = get_claude_client()
    default = {
        "trends": ["AI driving unprecedented demand", "Power constraints tightening", "Nuclear PPAs increasing"],
        "emerging_markets": ["Columbus OH", "Nashville TN", "Kansas City"],
        "risks": ["Grid capacity limits", "Permitting delays", "Labor shortages"],
        "recommendations": ["Add real-time pricing", "Expand queue data", "Build alert system"],
        "ai_generated": False
    }
    
    if not client:
        return default
    
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            messages=[{"role": "user", "content": f"Analyze data center market. Stats: {DCHUB_CONFIG}. Return JSON only with: trends (3), emerging_markets (3), risks (3), recommendations (3)"}]
        )
        text = response.content[0].text.strip()
        start = text.find('{')
        end = text.rfind('}') + 1
        if start >= 0 and end > start:
            result = json.loads(text[start:end])
            result["ai_generated"] = True
            return result
    except Exception as e:
        print(f"Analysis error: {e}")
    
    return default

# Generate daily report
def generate_daily_report():
    analysis = generate_market_analysis()
    agent_cache["market_analysis"] = analysis
    
    trends = analysis.get('trends', ["AI demand surge", "Power constraints", "Nuclear PPAs"])
    emerging = analysis.get('emerging_markets', ["Columbus OH", "Nashville TN", "Kansas City"])
    risks = analysis.get('risks', ["Grid limits", "Permitting delays", "Labor shortages"])
    ai_status = "Claude Active" if analysis.get('ai_generated') else "Using cached analysis"
    
    lines = [
        "",
        "━" * 50,
        "🤖 DC HUB AI AGENT - DAILY REPORT",
        f"📅 {datetime.now().strftime('%A, %B %d, %Y at %I:%M %p')}",
        "━" * 50,
        "",
        "📊 PLATFORM STATUS",
        f"• Version: {DCHUB_CONFIG['version']}",
        f"• Facilities: {_fmt(DCHUB_CONFIG['facilities_count'])}",
        f"• Markets: {DCHUB_CONFIG['markets_count']}",
        f"• Pipeline: {_fmt(DCHUB_CONFIG['pipeline_gw'], ' GW')}",
        f"• Vacancy: {_fmt(DCHUB_CONFIG['vacancy_rate'], '%')}",
        "",
        "🔥 TOP MARKET TRENDS",
    ]
    for t in trends[:3]:
        lines.append(f"• {t}")
    
    lines.append("")
    lines.append("🌍 EMERGING MARKETS")
    for m in emerging[:3]:
        lines.append(f"• {m}")
    
    lines.append("")
    lines.append("⚠️ RISK FACTORS")
    for r in risks[:3]:
        lines.append(f"• {r}")
    
    lines.extend(["", f"🤖 AI Status: {ai_status}", "", "━" * 50])
    
    report = "\n".join(lines)
    
    agent_cache["reports"].append({
        "timestamp": datetime.now().isoformat(),
        "report": report,
        "analysis": analysis
    })
    
    if len(agent_cache["reports"]) > 7:
        agent_cache["reports"].pop(0)
    
    agent_cache["last_updates"]["report"] = datetime.now().isoformat()
    return report

# Generate LinkedIn post
def generate_linkedin_post(post_type="stats"):
    templates = {
        "stats": f"🚀 DC Hub tracks {_fmt(DCHUB_CONFIG['facilities_count'])} data centers across 170+ countries.\n\n📊 {_fmt(DCHUB_CONFIG['pipeline_gw'])} GW under construction\n📉 {DCHUB_CONFIG['vacancy_rate']}% vacancy\n💰 {DCHUB_CONFIG['avg_pricing']} avg pricing\n\n#DataCenter #Infrastructure #AI",
        "pipeline": f"🏗️ {_fmt(DCHUB_CONFIG['pipeline_gw'])} GW under development.\n\nTop markets:\n🔹 Northern Virginia: 5.9 GW\n🔹 Phoenix: 4.2 GW\n🔹 Dallas: 3.9 GW\n\n#DataCenter #Construction",
        "v68": f"🚀 DC Hub v68 Released!\n\n⏳ 1.3 TW Gen Queue\n🏭 8 Midstream Gas Operators\n⛽ 10 LNG Terminals\n🔗 6 Long-Haul Fiber Carriers\n🌐 64 Markets\n\n#DataCenter #SiteSelection"
    }
    return templates.get(post_type, templates["stats"])

# Update news
def auto_update_agent_news():
    print(f"🤖 [{datetime.now().strftime('%H:%M:%S')}] Updating news...")
    
    all_articles = []
    keywords = ["data center", "datacenter", "hyperscale", "colocation", "aws", "google cloud", "microsoft azure"]
    
    for source in NEWS_SOURCES:
        try:
            feed = feedparser.parse(source["url"])
            for entry in feed.entries[:8]:
                title = entry.get("title", "").lower()
                summary = entry.get("summary", "").lower()
                is_relevant = any(kw in title or kw in summary for kw in keywords)
                
                all_articles.append({
                    "title": entry.get("title", "")[:150],
                    "link": entry.get("link", ""),
                    "source": source["name"],
                    "category": source["category"],
                    "published": entry.get("published", ""),
                    "summary": (entry.get("summary", "") or "")[:200],
                    "relevant": is_relevant,
                    "fetched_at": datetime.now().isoformat()
                })
        except Exception as e:
            print(f"  ⚠️ {source['name']}: {str(e)[:30]}")
    
    seen = set()
    unique = []
    for a in sorted(all_articles, key=lambda x: x.get("published", ""), reverse=True):
        if a["title"] not in seen:
            seen.add(a["title"])
            unique.append(a)
    
    agent_cache["news"] = unique[:50]
    agent_cache["last_updates"]["news"] = datetime.now().isoformat()
    print(f"  ✅ {len(agent_cache['news'])} articles loaded")

# Dashboard HTML
DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>DC Hub AI Agent</title>
    <style>
        *{margin:0;padding:0;box-sizing:border-box}
        body{font-family:system-ui,sans-serif;background:#0a0a12;color:#e2e8f0;padding:24px}
        .container{max-width:900px;margin:0 auto}
        h1{margin-bottom:8px}h1 span{color:#818cf8}
        .subtitle{color:#64748b;margin-bottom:24px}
        .card{background:#12121a;border:1px solid #1e1e2e;border-radius:12px;padding:20px;margin-bottom:16px}
        .card h3{color:#64748b;font-size:14px;margin-bottom:8px}
        .value{font-size:2rem;font-weight:700;color:#818cf8}
        .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:16px;margin-bottom:24px}
        .btn{padding:10px 20px;background:#6366f1;color:white;border:none;border-radius:8px;cursor:pointer;margin-right:8px;margin-bottom:8px}
        .btn:hover{background:#4f46e5}
        pre{background:#0a0a12;padding:16px;border-radius:8px;overflow-x:auto;font-size:12px;white-space:pre-wrap}
        .status{display:inline-flex;align-items:center;gap:6px;padding:4px 12px;background:rgba(16,185,129,0.1);border-radius:20px;font-size:12px;color:#10b981}
        .status::before{content:'';width:8px;height:8px;background:#10b981;border-radius:50%}
    </style>
</head>
<body>
    <div class="container">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:24px">
            <div><h1>🤖 DC Hub <span>AI Agent</span></h1><p class="subtitle">Powered by Claude</p></div>
            <span class="status">Active</span>
        </div>
        <div class="grid">
            <div class="card"><h3>📊 Facilities</h3><div class="value">{{ facilities }}</div></div>
            <div class="card"><h3>🌍 Markets</h3><div class="value">{{ markets }}</div></div>
            <div class="card"><h3>🏗️ Pipeline</h3><div class="value">{{ pipeline }}GW</div></div>
            <div class="card"><h3>📰 News</h3><div class="value">{{ news_count }}</div></div>
        </div>
        <div class="card">
            <h3>⚡ Actions</h3>
            <button class="btn" onclick="trigger('news')">🔄 Refresh News</button>
            <button class="btn" onclick="trigger('report')">📋 Generate Report</button>
            <button class="btn" onclick="window.open('/api/agent/report')">📄 View Report</button>
        </div>
        <div class="card">
            <h3>📋 Latest Report</h3>
            <pre>{{ report }}</pre>
        </div>
    </div>
    <script>
        async function trigger(task) {
            try {
                const r = await fetch('/api/agent/trigger/' + task, {method: 'POST'});
                const d = await r.json();
                alert('✅ ' + task + ' done!');
                location.reload();
            } catch(e) {
                alert('Error: ' + e.message);
            }
        }
    </script>
</body>
</html>
"""

# Routes
# AUTO-REPAIR: duplicate route '/' also in main.py:23194 — review and remove one
@ai_agent.route('/')
def dashboard():
    report = agent_cache["reports"][-1]["report"] if agent_cache["reports"] else "No reports yet. Click 'Generate Report'."
    return render_template_string(DASHBOARD_HTML,
        facilities=_fmt(DCHUB_CONFIG['facilities_count']),
        markets=DCHUB_CONFIG['markets_count'],
        pipeline=DCHUB_CONFIG['pipeline_gw'],
        news_count=len(agent_cache["news"]),
        report=report
    )
# AUTO-REPAIR: duplicate route '/status' also in enhanced_promotion.py:826 — review and remove one

@ai_agent.route('/status')
def status():
    return jsonify({
        "status": "running",
        "version": DCHUB_CONFIG["version"],
        "claude_enabled": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "last_updates": agent_cache["last_updates"],
        "counts": {"news": len(agent_cache["news"]), "reports": len(agent_cache["reports"])}
# AUTO-REPAIR: duplicate route '/news' also in main.py:24457 — review and remove one
    })

@ai_agent.route('/news')
def news():
    relevant = request.args.get('relevant', 'false').lower() == 'true'
    limit = request.args.get('limit', 30, type=int)
    articles = [a for a in agent_cache["news"] if a.get('relevant')] if relevant else agent_cache["news"]
    return jsonify({"articles": articles[:limit], "total": len(articles), "last_update": agent_cache["last_updates"].get("news")})

@ai_agent.route('/report')
def report():
    if agent_cache["reports"]:
        return agent_cache["reports"][-1]["report"], 200, {'Content-Type': 'text/plain; charset=utf-8'}
    return "No reports yet.", 404

@ai_agent.route('/analysis')
def analysis():
    if not agent_cache["market_analysis"]:
        agent_cache["market_analysis"] = generate_market_analysis()
    return jsonify(agent_cache["market_analysis"])

@ai_agent.route('/linkedin/<post_type>')
def linkedin(post_type):
    return jsonify({"post_type": post_type, "content": generate_linkedin_post(post_type), "generated_at": datetime.now().isoformat()})

@ai_agent.route('/trigger/<task>', methods=['POST'])
def trigger(task):
    try:
        if task == 'news':
            auto_update_agent_news()
            return jsonify({"status": "ok", "task": task, "count": len(agent_cache["news"])})
        elif task == 'report':
            report = generate_daily_report()
            return jsonify({"status": "ok", "task": task, "length": len(report)})
        elif task == 'analysis':
            agent_cache["market_analysis"] = generate_market_analysis()
            return jsonify({"status": "ok", "task": task})
        return jsonify({"error": "Unknown task"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Chat endpoint
def get_chat_prompt():
    """Generate dynamic chat prompt with live stats"""
    config = get_live_dchub_config()
    return f"""You are DC Hub AI assistant. DC Hub (dchub.cloud) tracks {_fmt(config['facilities_count'])} data centers across 170+ countries, {_fmt(config['markets_count'])} markets. Current vacancy: {_fmt(config['vacancy_rate'])}%. Pipeline: {_fmt(config['pipeline_gw'])} GW. Key markets: Northern Virginia (largest), Phoenix (fastest growing), Dallas, Chicago. Top providers: Equinix, Digital Realty, QTS. Keep responses concise, under 100 words."""

CHAT_PROMPT = get_chat_prompt()

@ai_agent.route('/chat', methods=['POST'])
def chat():
    try:
        data = request.get_json()
        message = data.get('message', '').strip()
        if not message:
            return jsonify({"error": "No message"}), 400
        
        client = get_claude_client()
        if not client:
            return jsonify({"response": "AI chat requires API key. Contact info@dchub.cloud for help.", "model": "fallback"})
        
        from utils.anthropic_helper import cached_system  # cache the static CHAT_PROMPT prefix
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=cached_system(CHAT_PROMPT),
            messages=[{"role": "user", "content": message}]
        )
        return jsonify({"response": response.content[0].text, "model": "claude-sonnet-4"})
    except Exception as e:
        return jsonify({"response": f"Error: {str(e)}", "model": "error"}), 500

# Scheduler
scheduler = None

def setup_ai_agent(app):
    global scheduler
    app.register_blueprint(ai_agent)
    
    scheduler = BackgroundScheduler()
    scheduler.add_job(auto_update_agent_news, 'interval', minutes=10, id='news')
    scheduler.start()
    
    auto_update_agent_news()
    
    print("=" * 50)
    print("🤖 DC Hub AI Agent initialized!")
    print("=" * 50)
    print("  Dashboard: /api/agent/")
    print("  News: /api/agent/news")
    print("  Report: /api/agent/report")
    print("  Chat: /api/agent/chat")
    print(f"  Claude: {'✅' if os.environ.get('ANTHROPIC_API_KEY') else '❌ Add ANTHROPIC_API_KEY'}")
    print("=" * 50)
    return app

if __name__ == '__main__':
    from flask import Flask
    app = Flask(__name__)
    setup_ai_agent(app)
    app.run(host='0.0.0.0', port=5001)
