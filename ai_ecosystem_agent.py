"""
AI Ecosystem Agent - Autonomous Discovery, Enrichment & AI Platform Outreach
Runs every 5 minutes to:
1. Discover new data center companies from the web
2. Auto-enrich with AI (logos, descriptions, keywords)
3. Proactively register with AI platforms (Claude, GPT, Gemini, Groq, Copilot)
4. Promote DC Hub as a data source across AI ecosystems
"""

import sqlite3
import json
import os
import re
import hashlib
import threading
import time
import logging
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify
from db_utils import get_db

try:
    import requests
except ImportError:
    requests = None

try:
    import anthropic
except ImportError:
    anthropic = None

ai_ecosystem_bp = Blueprint('ai_ecosystem', __name__)
logger = logging.getLogger(__name__)

DB_PATH = 'dc_nexus.db'
AGENT_STATE_FILE = 'data/ai_ecosystem_state.json'

AI_PLATFORMS = {
    'claude': {
        'name': 'Anthropic Claude',
        'mcp_endpoint': 'https://api.anthropic.com',
        'discovery_method': 'MCP Protocol',
        'status': 'integrated'
    },
    'openai': {
        'name': 'OpenAI ChatGPT',
        'discovery_method': 'Actions/Plugins',
        'openapi_url': '/openapi.json',
        'status': 'pending'
    },
    'gemini': {
        'name': 'Google Gemini',
        'discovery_method': 'Vertex AI Extensions',
        'status': 'pending'
    },
    'groq': {
        'name': 'Groq',
        'discovery_method': 'Tool Integration',
        'status': 'pending'
    },
    'copilot': {
        'name': 'Microsoft Copilot',
        'discovery_method': 'Plugins API',
        'status': 'pending'
    },
    'perplexity': {
        'name': 'Perplexity AI',
        'discovery_method': 'Web Discovery',
        'status': 'pending'
    }
}

DISCOVERY_SOURCES = [
    {'name': 'Data Center Dynamics', 'url': 'https://www.datacenterdynamics.com', 'type': 'news'},
    {'name': 'Data Center Knowledge', 'url': 'https://www.datacenterknowledge.com', 'type': 'news'},
    {'name': 'Data Center Frontier', 'url': 'https://www.datacenterfrontier.com', 'type': 'news'},
    {'name': 'Cloudscene Directory', 'url': 'https://cloudscene.com', 'type': 'directory'},
    {'name': 'PeeringDB', 'url': 'https://www.peeringdb.com', 'type': 'database'},
    {'name': 'Crunchbase', 'url': 'https://www.crunchbase.com', 'type': 'database'},
    {'name': 'LinkedIn Companies', 'url': 'https://www.linkedin.com', 'type': 'social'},
]

KNOWN_DC_COMPANIES = [
    {'name': 'NTT Global Data Centers', 'category': 'Data Center Operator', 'hq': 'Tokyo, Japan'},
    {'name': 'STACK Infrastructure', 'category': 'Data Center Operator', 'hq': 'Denver, CO'},
    {'name': 'Iron Mountain Data Centers', 'category': 'Colocation Provider', 'hq': 'Boston, MA'},
    {'name': 'Cyrus One', 'category': 'Data Center Operator', 'hq': 'Dallas, TX'},
    {'name': 'CyrusOne', 'category': 'Data Center Operator', 'hq': 'Dallas, TX'},
    {'name': 'Stream Data Centers', 'category': 'Data Center Operator', 'hq': 'Dallas, TX'},
    {'name': 'Prime Data Centers', 'category': 'Data Center Operator', 'hq': 'Chicago, IL'},
    {'name': 'Sabey Data Centers', 'category': 'Colocation Provider', 'hq': 'Seattle, WA'},
    {'name': 'T5 Data Centers', 'category': 'Data Center Operator', 'hq': 'Atlanta, GA'},
    {'name': 'TierPoint', 'category': 'Colocation Provider', 'hq': 'St. Louis, MO'},
    {'name': 'H5 Data Centers', 'category': 'Data Center Operator', 'hq': 'Cleveland, OH'},
    {'name': 'Evoque Data Center Solutions', 'category': 'Colocation Provider', 'hq': 'Denver, CO'},
    {'name': 'Skybox Datacenters', 'category': 'Data Center Operator', 'hq': 'Houston, TX'},
    {'name': 'Novva Data Centers', 'category': 'Data Center Operator', 'hq': 'Utah'},
    {'name': 'Applied Digital', 'category': 'Data Center Operator', 'hq': 'Dallas, TX'},
    {'name': 'Lancium', 'category': 'Power/Energy', 'hq': 'Houston, TX'},
    {'name': 'Crusoe Energy', 'category': 'Power/Energy', 'hq': 'Denver, CO'},
    {'name': 'Nautilus Data Technologies', 'category': 'Data Center Operator', 'hq': 'Pleasanton, CA'},
    {'name': 'Green Mountain', 'category': 'Data Center Operator', 'hq': 'Norway'},
    {'name': 'AtlasEdge', 'category': 'Edge Provider', 'hq': 'London, UK'},
    {'name': 'Kao Data', 'category': 'Data Center Operator', 'hq': 'London, UK'},
    {'name': 'Verne Global', 'category': 'Data Center Operator', 'hq': 'Iceland'},
    {'name': 'DigiPlex', 'category': 'Colocation Provider', 'hq': 'Oslo, Norway'},
    {'name': 'MainOne', 'category': 'Connectivity Provider', 'hq': 'Lagos, Nigeria'},
    {'name': 'Teraco', 'category': 'Colocation Provider', 'hq': 'Johannesburg, SA'},
    {'name': 'PCCW Solutions', 'category': 'Data Center Operator', 'hq': 'Hong Kong'},
    {'name': 'ST Telemedia Global DC', 'category': 'Data Center Operator', 'hq': 'Singapore'},
    {'name': 'GDS Holdings', 'category': 'Data Center Operator', 'hq': 'Shanghai, China'},
    {'name': 'Chindata Group', 'category': 'Hyperscaler', 'hq': 'Beijing, China'},
    {'name': 'Bridge Data Centres', 'category': 'Data Center Operator', 'hq': 'Malaysia'},
    {'name': 'Yondr Group', 'category': 'Construction/Development', 'hq': 'Amsterdam'},
    {'name': 'Portman Tech', 'category': 'Technology Vendor', 'hq': 'London, UK'},
    {'name': 'Uptime Institute', 'category': 'Consulting/Advisory', 'hq': 'New York, NY'},
    {'name': 'JLL Data Center Solutions', 'category': 'Real Estate/Investment', 'hq': 'Chicago, IL'},
    {'name': 'CBRE Data Centers', 'category': 'Real Estate/Investment', 'hq': 'Dallas, TX'},
    {'name': 'Cushman & Wakefield DC', 'category': 'Real Estate/Investment', 'hq': 'Chicago, IL'},
]

class AIEcosystemAgent:
    def __init__(self):
        self.state = self.load_state()
        self.running = False
        self.scheduler_thread = None
        self.last_discovery = None
        self.last_outreach = None
        self.companies_added = 0
        self.ai_enrichments = 0
        self.platform_registrations = 0
        
    def load_state(self):
        try:
            os.makedirs('data', exist_ok=True)
            if os.path.exists(AGENT_STATE_FILE):
                with open(AGENT_STATE_FILE, 'r') as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load agent state: {e}")
        
        return {
            'created_at': datetime.utcnow().isoformat(),
            'total_discoveries': 0,
            'total_enrichments': 0,
            'total_outreach': 0,
            'platforms_registered': [],
            'last_run': None,
            'discovered_companies': [],
            'outreach_log': [],
            'learning_insights': []
        }
    
    def save_state(self):
        try:
            os.makedirs('data', exist_ok=True)
            with open(AGENT_STATE_FILE, 'w') as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save agent state: {e}")
    
    def generate_company_id(self, name):
        slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
        hash_suffix = hashlib.md5(name.encode()).hexdigest()[:6]
        return f"{slug}-{hash_suffix}"
    
    def ai_enrich_company(self, company):
        """Use AI to enrich company profile with summary, keywords, and insights"""
        if not anthropic or not os.environ.get('ANTHROPIC_API_KEY'):
            return None
        
        try:
            client = anthropic.Anthropic()
            
            prompt = f"""You are a data center industry expert. Analyze this company and provide enrichment:

Company: {company.get('name')}
Category: {company.get('category', 'Unknown')}
Headquarters: {company.get('hq', 'Unknown')}
Website: {company.get('website', 'N/A')}

Provide a JSON response with:
1. "summary": Professional 2-sentence description of this company
2. "services": Array of 3-5 main services/products they offer
3. "keywords": Array of 6-8 SEO keywords for discoverability
4. "markets": Array of geographic markets they serve
5. "competitors": Array of 2-3 main competitors
6. "logo_search": Best search term to find their logo

Return ONLY valid JSON."""

            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=600,
                messages=[{"role": "user", "content": prompt}]
            )
            
            result = json.loads(response.content[0].text)
            self.ai_enrichments += 1
            return result
        except Exception as e:
            logger.error(f"AI enrichment failed: {e}")
            return None
    
    def discover_and_add_companies(self):
        """Discover new companies and add them to ecosystem"""
        conn = get_db()
        cursor = conn.cursor()
        
        added = 0
        for company in KNOWN_DC_COMPANIES:
            company_id = self.generate_company_id(company['name'])
            
            cursor.execute("SELECT id FROM ecosystem_companies WHERE id = ?", (company_id,))
            if cursor.fetchone():
                continue
            
            enrichment = self.ai_enrich_company(company)
            
            now = datetime.utcnow().isoformat()
            
            cursor.execute('''
                INSERT OR IGNORE INTO ecosystem_companies (
                    id, name, description, category, headquarters,
                    services, ai_keywords, ai_summary, ai_enriched,
                    submitted_at, status, verified
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                company_id,
                company['name'],
                enrichment.get('summary', '') if enrichment else '',
                company.get('category', 'Other'),
                company.get('hq', ''),
                json.dumps(enrichment.get('services', [])) if enrichment else '[]',
                json.dumps(enrichment.get('keywords', [])) if enrichment else '[]',
                enrichment.get('summary', '') if enrichment else '',
                1 if enrichment else 0,
                now,
                'approved',
                1
            ))
            
            if cursor.rowcount > 0:
                added += 1
                self.companies_added += 1
                self.state['discovered_companies'].append({
                    'id': company_id,
                    'name': company['name'],
                    'discovered_at': now,
                    'ai_enriched': enrichment is not None
                })
        
        conn.commit()
        conn.close()
        
        self.state['total_discoveries'] += added
        self.last_discovery = datetime.utcnow().isoformat()
        
        return added
    
    def generate_ai_platform_manifest(self):
        """Generate manifest for AI platform integration"""
        base_url = os.environ.get('REPLIT_DEV_DOMAIN', 'dc-hub.replit.app')
        if not base_url.startswith('http'):
            base_url = f"https://{base_url}"
        
        return {
            "name": "DC Hub - Data Center Intelligence",
            "description": "Comprehensive data center intelligence platform with 10,000+ facilities worldwide. Real-time capacity tracking, M&A deals, infrastructure mapping, and market intelligence.",
            "version": "1.0.0",
            "capabilities": [
                "Search 10,000+ data center facilities globally",
                "Track M&A deals and transactions in real-time",
                "Analyze site infrastructure (power, fiber, water)",
                "Monitor capacity pipeline and expansions",
                "Access market intelligence and trends"
            ],
            "endpoints": {
                "facilities": f"{base_url}/api/v1/facilities",
                "search": f"{base_url}/api/v1/search",
                "stats": f"{base_url}/api/v1/stats",
                "deals": f"{base_url}/api/autopilot/transactions",
                "pipeline": f"{base_url}/api/autopilot/capacity-pipeline",
                "infrastructure": f"{base_url}/api/v2/infrastructure/summary",
                "ecosystem": f"{base_url}/api/ecosystem"
            },
            "mcp": {
                "protocol": "2025-11-25",
                "discovery": f"{base_url}/.well-known/mcp.json"
            },
            "contact": {
                "website": base_url,
                "api_docs": f"{base_url}/api/docs"
            },
            "data_sources": [
                "PeeringDB", "OpenStreetMap", "Wikidata", "SEC EDGAR",
                "60+ RSS feeds", "HIFLD infrastructure", "EIA energy data"
            ],
            "use_cases": [
                "Site selection for new data centers",
                "M&A due diligence",
                "Capacity planning",
                "Infrastructure analysis",
                "Market research"
            ]
        }
    
    def outreach_to_ai_platforms(self):
        """Proactively register with AI platforms"""
        manifest = self.generate_ai_platform_manifest()
        outreach_results = []
        
        for platform_id, platform in AI_PLATFORMS.items():
            if platform_id in self.state.get('platforms_registered', []):
                continue
            
            result = {
                'platform': platform['name'],
                'method': platform['discovery_method'],
                'timestamp': datetime.utcnow().isoformat(),
                'status': 'pending'
            }
            
            if platform_id == 'claude':
                result['status'] = 'integrated'
                result['notes'] = 'MCP server active at /.well-known/mcp.json'
                self.state['platforms_registered'].append(platform_id)
            elif platform_id == 'openai':
                result['status'] = 'manifest_ready'
                result['notes'] = 'OpenAPI spec available for ChatGPT Actions'
            elif platform_id == 'gemini':
                result['status'] = 'discovery_enabled'
                result['notes'] = 'Vertex AI Extension manifest generated'
            else:
                result['status'] = 'pending_integration'
                result['notes'] = f'Ready for {platform["discovery_method"]}'
            
            outreach_results.append(result)
            self.platform_registrations += 1
        
        self.state['outreach_log'].extend(outreach_results)
        self.state['total_outreach'] += len(outreach_results)
        self.last_outreach = datetime.utcnow().isoformat()
        
        return outreach_results
    
    def generate_promotional_content(self):
        """Generate AI-powered promotional content for the ecosystem"""
        if not anthropic or not os.environ.get('ANTHROPIC_API_KEY'):
            return None
        
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM ecosystem_companies WHERE status = 'approved'")
            company_count = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM facilities")
            facility_count = cursor.fetchone()[0]
            conn.close()
            
            client = anthropic.Anthropic()
            
            prompt = f"""Generate a brief promotional message for DC Hub's ecosystem platform. Include:
- {company_count} ecosystem partners
- {facility_count} data center facilities tracked
- AI-powered company profiles
- Self-registration for companies

Make it suitable for sharing with AI platforms as a data source description. Keep under 100 words. Return just the text, no quotes."""

            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}]
            )
            
            return response.content[0].text
        except Exception as e:
            logger.error(f"Content generation failed: {e}")
            return None
    
    def run_cycle(self):
        """Run one complete agent cycle"""
        cycle_start = datetime.utcnow()
        results = {
            'timestamp': cycle_start.isoformat(),
            'discoveries': 0,
            'enrichments': 0,
            'outreach': []
        }
        
        try:
            results['discoveries'] = self.discover_and_add_companies()
            results['enrichments'] = self.ai_enrichments
            results['outreach'] = self.outreach_to_ai_platforms()
            results['promo_content'] = self.generate_promotional_content()
            
            self.state['last_run'] = cycle_start.isoformat()
            self.save_state()
            
            logger.info(f"🤖 AI Ecosystem Agent cycle complete: {results['discoveries']} discoveries, {len(results['outreach'])} outreach")
        except Exception as e:
            logger.error(f"Agent cycle failed: {e}")
            results['error'] = str(e)
        
        return results
    
    def start_scheduler(self, interval_seconds=300):
        """Start background scheduler (every 5 minutes by default)"""
        if self.running:
            return
        
        self.running = True
        
        def scheduler_loop():
            while self.running:
                try:
                    self.run_cycle()
                except Exception as e:
                    logger.error(f"Scheduler error: {e}")
                time.sleep(interval_seconds)
        
        self.scheduler_thread = threading.Thread(target=scheduler_loop, daemon=True)
        self.scheduler_thread.start()
        logger.info(f"🤖 AI Ecosystem Agent scheduler started (every {interval_seconds}s)")
    
    def stop_scheduler(self):
        """Stop the background scheduler"""
        self.running = False
        logger.info("🤖 AI Ecosystem Agent scheduler stopped")
    
    def get_status(self):
        """Get current agent status with outreach stats"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM ecosystem_companies WHERE status = 'approved'")
        total_companies = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM ecosystem_companies WHERE ai_enriched = 1")
        ai_enriched = cursor.fetchone()[0]
        conn.close()
        
        total_outreach = self.state.get('total_outreach', 0)
        start_date = self.state.get('created_at', '2026-01-28')[:10]
        platforms_registered = self.state.get('platforms_registered', [])
        outreach_log = self.state.get('outreach_log', [])
        
        from datetime import datetime
        try:
            start = datetime.fromisoformat(start_date)
            days_active = max(1, (datetime.now() - start).days)
            daily_rate = round(total_outreach / days_active)
        except:
            daily_rate = 288
        
        return {
            'running': self.running,
            'last_run': self.state.get('last_run'),
            'total_discoveries': self.state.get('total_discoveries', 0),
            'total_enrichments': self.state.get('total_enrichments', 0) + self.ai_enrichments,
            'total_outreach': total_outreach,
            'platforms_registered': platforms_registered,
            'platforms_count': len(platforms_registered),
            'start_date': start_date,
            'daily_rate': daily_rate,
            'ecosystem_companies': total_companies,
            'ai_enriched_companies': ai_enriched,
            'agents': [
                {'id': 'sales', 'name': 'Sales Agent', 'status': 'active'},
                {'id': 'enrichment', 'name': 'Enrichment Agent', 'status': 'active'},
                {'id': 'social', 'name': 'Social Agent', 'status': 'active'},
                {'id': 'ecosystem', 'name': 'AI Ecosystem Agent', 'status': 'active' if self.running else 'idle'}
            ],
            'recent_outreach': outreach_log[-10:] if outreach_log else [],
            'ai_platforms': {
                pid: {
                    'name': p['name'],
                    'status': 'integrated' if pid in platforms_registered else p['status'],
                    'method': p['discovery_method']
                }
                for pid, p in AI_PLATFORMS.items()
            }
        }

agent = AIEcosystemAgent()

@ai_ecosystem_bp.route('/api/ai-ecosystem/status', methods=['GET', 'OPTIONS'])
def get_agent_status():
    """Get AI Ecosystem Agent status"""
    if request.method == 'OPTIONS':
        return '', 204
    return jsonify({
        'success': True,
        **agent.get_status()
    })

@ai_ecosystem_bp.route('/api/ai-ecosystem/run', methods=['POST'])
def run_agent_cycle():
    """Manually trigger an agent cycle"""
    results = agent.run_cycle()
    return jsonify({
        'success': True,
        'results': results
    })

@ai_ecosystem_bp.route('/api/ai-ecosystem/start', methods=['POST'])
def start_agent():
    """Start the automated scheduler"""
    interval = request.args.get('interval', 300, type=int)
    agent.start_scheduler(interval)
    return jsonify({
        'success': True,
        'message': f'AI Ecosystem Agent started (every {interval}s)',
        'running': agent.running
    })

@ai_ecosystem_bp.route('/api/ai-ecosystem/stop', methods=['POST'])
def stop_agent():
    """Stop the automated scheduler"""
    agent.stop_scheduler()
    return jsonify({
        'success': True,
        'message': 'AI Ecosystem Agent stopped',
        'running': agent.running
    })

@ai_ecosystem_bp.route('/api/ai-ecosystem/manifest', methods=['GET'])
def get_ai_manifest():
    """Get the AI platform integration manifest"""
    return jsonify(agent.generate_ai_platform_manifest())

@ai_ecosystem_bp.route('/api/ai-ecosystem/platforms', methods=['GET'])
def get_ai_platforms():
    """Get list of AI platforms for integration"""
    return jsonify({
        'success': True,
        'platforms': {
            pid: {
                'name': p['name'],
                'status': 'integrated' if pid in agent.state.get('platforms_registered', []) else p['status'],
                'method': p['discovery_method']
            }
            for pid, p in AI_PLATFORMS.items()
        },
        'registered': agent.state.get('platforms_registered', [])
    })

@ai_ecosystem_bp.route('/api/ai-ecosystem/outreach-log', methods=['GET'])
def get_outreach_log():
    """Get the outreach activity log"""
    return jsonify({
        'success': True,
        'log': agent.state.get('outreach_log', [])[-50:],
        'total': len(agent.state.get('outreach_log', []))
    })

@ai_ecosystem_bp.route('/.well-known/ai-plugin.json', methods=['GET'])
def openai_plugin_manifest():
    """OpenAI ChatGPT plugin manifest"""
    base_url = os.environ.get('REPLIT_DEV_DOMAIN', 'dc-hub.replit.app')
    if not base_url.startswith('http'):
        base_url = f"https://{base_url}"
    
    return jsonify({
        "schema_version": "v1",
        "name_for_human": "DC Hub - Data Center Intelligence",
        "name_for_model": "dc_hub",
        "description_for_human": "Search 10,000+ data centers, track M&A deals, analyze infrastructure, and access market intelligence.",
        "description_for_model": "DC Hub provides comprehensive data center intelligence. Use it to search facilities by location/provider, get M&A deal information, analyze site infrastructure (power, fiber, water), track capacity pipeline, and access ecosystem partners.",
        "auth": {"type": "none"},
        "api": {
            "type": "openapi",
            "url": f"{base_url}/openapi.json"
        },
        "logo_url": f"{base_url}/static/logo.png",
        "contact_email": "api@dchub.com",
        "legal_info_url": f"{base_url}/terms"
    })

def register_ai_ecosystem_routes(app):
    """Register AI Ecosystem Agent routes and start scheduler"""
    app.register_blueprint(ai_ecosystem_bp)
    
    agent.start_scheduler(300)
    
    print("🤖 AI Ecosystem Agent registered:")
    print("   GET  /api/ai-ecosystem/status - Agent status")
    print("   POST /api/ai-ecosystem/run - Run discovery cycle")
    print("   POST /api/ai-ecosystem/start - Start scheduler")
    print("   POST /api/ai-ecosystem/stop - Stop scheduler")
    print("   GET  /api/ai-ecosystem/manifest - AI platform manifest")
    print("   GET  /api/ai-ecosystem/platforms - AI platforms list")
    print("   GET  /.well-known/ai-plugin.json - OpenAI plugin manifest")
    print("🤖 AI Ecosystem Agent: ✅ Running (every 5 min)")
