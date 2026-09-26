"""
AI Ecosystem Agent - Autonomous Discovery, Enrichment & AI Platform Outreach
Runs every 5 minutes to:
1. Discover new data center companies from the web
2. Auto-enrich with AI (logos, descriptions, keywords)
3. Proactively register with AI platforms (Claude, GPT, Gemini, Groq, Copilot)
4. Promote DC Hub as a data source across AI ecosystems
"""

import json
import os
import re
import hashlib
import tempfile
import threading
import time
import logging
from datetime import datetime, timedelta, timezone
from flask import Blueprint, request, jsonify
from internal_auth import is_valid_internal_key
from db_utils import get_db
from ai_surface_canon import canon_text

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
# Redirectable so a test never writes the TRACKED 2.1 MB file at the default
# path. Same knob shape as DCHUB_AMBASSADOR_STATE_FILE (#3014); the default is
# unchanged, so nothing about a real deployment moves.
AGENT_STATE_FILE = os.environ.get(
    'DCHUB_AI_ECOSYSTEM_STATE_FILE', 'data/ai_ecosystem_state.json')

# One writer at a time. The scheduler thread and POST /api/ai-ecosystem/run
# share the single module-level `agent` and the single state file, so two
# overlapping open(AGENT_STATE_FILE, 'w') calls truncate and interleave.
_STATE_LOCK = threading.Lock()


def _mkstatedir():
    """Ensure AGENT_STATE_FILE's directory exists (it may be a redirected tmp
    path, so this cannot hardcode 'data')."""
    d = os.path.dirname(AGENT_STATE_FILE)
    if d:
        os.makedirs(d, exist_ok=True)

AI_PLATFORMS = {
    'claude': {
        'name': 'Anthropic Claude',
        'mcp_endpoint': 'https://gateway.ai.cloudflare.com/v1/4bb33ec40ef02f9f4b41dc97668d5a52/dchub/anthropic',
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
        """Load persisted state. An ABSENT file is an ordinary fresh start; an
        unreadable one is not.

        The old body collapsed both cases into the empty default, which is the
        step that turns one torn write into permanent loss — the next
        save_state() persists the blank over the accumulated history. A file
        that exists but will not parse is now moved aside intact, so the bytes
        survive and the failure is loud.
        """
        try:
            _mkstatedir()
        except Exception as e:
            logger.error(f"Agent state dir unavailable: {e}")
        if os.path.exists(AGENT_STATE_FILE):
            try:
                with open(AGENT_STATE_FILE, 'r') as f:
                    return json.load(f)
            except Exception as e:
                quarantine = AGENT_STATE_FILE + '.corrupt'
                try:
                    os.replace(AGENT_STATE_FILE, quarantine)
                except OSError as move_err:
                    quarantine = f'<could not preserve: {move_err}>'
                logger.error(
                    "Agent state at %s is unreadable (%s); preserved as %s and "
                    "starting from empty. The accumulated history is in that "
                    "file, NOT lost.", AGENT_STATE_FILE, e, quarantine)

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
        """Persist state without ever exposing a half-written file.

        Serialise fully under the lock, write a sibling temp file, then
        os.replace() it into place — atomic on POSIX and Windows. A failure
        anywhere (including json.dumps raising because another thread mutated a
        list mid-serialise) leaves the PREVIOUS complete file untouched, where
        truncate-then-write left a corrupt one on disk.
        """
        tmp = None
        try:
            _mkstatedir()
            with _STATE_LOCK:
                blob = json.dumps(self.state, indent=2)
                d = os.path.dirname(AGENT_STATE_FILE) or '.'
                fd, tmp = tempfile.mkstemp(
                    dir=d, prefix='.ai_ecosystem_state.', suffix='.tmp')
                with os.fdopen(fd, 'w') as f:
                    f.write(blob)
                os.replace(tmp, AGENT_STATE_FILE)
                tmp = None
        except Exception as e:
            logger.error(f"Failed to save agent state: {e}")
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    
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
                model="claude-haiku-4-5-20251001",
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
        try:
            cursor = conn.cursor()

            added = 0
            for company in KNOWN_DC_COMPANIES:
                company_id = self.generate_company_id(company['name'])

                cursor.execute("SELECT id FROM ecosystem_companies WHERE id = %s", (company_id,))
                if cursor.fetchone():
                    continue

                enrichment = self.ai_enrich_company(company)

                now = datetime.utcnow().isoformat()

                cursor.execute('''
                    INSERT INTO ecosystem_companies (
                        id, name, description, category, headquarters,
                        services, ai_keywords, ai_summary, ai_enriched,
                        submitted_at, status, verified
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
        finally:
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
            "description": canon_text("Comprehensive data center intelligence platform with {canon_facilities} distinct facilities worldwide. Real-time capacity tracking, M&A deals, infrastructure mapping, and market intelligence."),
            "version": "1.0.0",
            "capabilities": [
                canon_text("Search {canon_facilities} distinct data center facilities globally"),
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
        """★ r-honest-outreach (2026-09-26): this used to be the "AI Outreach
        Engine". It contacted nobody. Each cycle it appended a canned note
        ("OpenAPI spec available for ChatGPT Actions", "Ready for Web
        Discovery", ...) for the same five platforms to outreach_log and added
        one to total_outreach per note. /ai published that counter as
        "10,021+ outreach pings sent · ~42/day" and the notes as "Recent
        Outreach Activity" -- 147 days stale by the time anyone asked why the
        outreach had stopped. It had never started.

        Real outbound lives elsewhere and is read by real_outreach_status():
        partner email (routes/ai_lab_outreach.py -> ai_lab_outreach_drafts),
        agent self-registration (routes/ai_platform_onboarder.py ->
        ai_platform_submissions) and directory listings
        (routes/mcp_registry_watch.py -> mcp_registry_probe_state).

        Kept as a no-op so run_cycle() and POST /api/ai-ecosystem/run keep
        their shape. It must never write outreach_log or total_outreach again.
        """
        return []

    def generate_promotional_content(self):
        """Generate AI-powered promotional content for the ecosystem"""
        if not anthropic or not os.environ.get('ANTHROPIC_API_KEY'):
            return None
        
        try:
            conn = get_db()
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM ecosystem_companies WHERE status = 'approved'")
                company_count = cursor.fetchone()[0]
                cursor.execute("SELECT COUNT(*) FROM facilities")
                facility_count = cursor.fetchone()[0]
            finally:
                conn.close()
            
            client = anthropic.Anthropic()
            
            prompt = f"""Generate a brief promotional message for DC Hub's ecosystem platform. Include:
- {company_count} ecosystem partners
- {facility_count} data center facilities tracked
- AI-powered company profiles
- Self-registration for companies

Make it suitable for sharing with AI platforms as a data source description. Keep under 100 words. Return just the text, no quotes."""

            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
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
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM ecosystem_companies WHERE status = 'approved'")
            total_companies = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM ecosystem_companies WHERE ai_enriched = 1")
            ai_enriched = cursor.fetchone()[0]
        finally:
            conn.close()
        
        platforms_registered = self.state.get('platforms_registered', [])
        # ★ r-honest-outreach: total_outreach / daily_rate / outreach_log were
        # the canned counter outreach_to_ai_platforms() used to walk (see its
        # docstring). They are no longer served. Everything below is read from
        # the ledgers real outreach writes.
        real = _cached_real_outreach()

        return {
            'running': self.running,
            'last_run': self.state.get('last_run'),
            'total_discoveries': self.state.get('total_discoveries', 0),
            'total_enrichments': self.state.get('total_enrichments', 0) + self.ai_enrichments,
            'platforms_registered': platforms_registered,
            'platforms_count': len(platforms_registered),
            'ecosystem_companies': total_companies,
            'ai_enriched_companies': ai_enriched,
            'outreach': real['summary'],
            'outreach_basis': (
                'Real outbound only: partner emails (ai_lab_outreach_drafts, '
                'status=sent), agent self-registrations '
                '(ai_platform_submissions) and directory listing checks '
                '(mcp_registry_probe_state). A lane is active only if its '
                'ledger moved within idle_after_days; a cron that fired and '
                'did nothing is idle.'),
            'idle_after_days': IDLE_AFTER_DAYS,
            'agents': real['lanes'],
            'recent_outreach': real['events'],
            'ai_platforms': {
                pid: {
                    'name': p['name'],
                    'status': 'integrated' if pid in platforms_registered else p['status'],
                    'method': p['discovery_method']
                }
                for pid, p in AI_PLATFORMS.items()
            }
        }

# ── r-honest-outreach (2026-09-26): what /ai calls "outreach", measured ──────
# A lane is `active` only when its ledger moved inside IDLE_AFTER_DAYS. Firing
# on schedule is not activity: on 2026-09-25/26 both outreach crons logged
# success with sent=0 candidates=0 and processed=0. `unknown` means the ledger
# could not be read and is never painted as running.
IDLE_AFTER_DAYS = {'directories': 3, 'partner_email': 14, 'self_registration': 14}

_DIRECTORY_COPY_NOTE = {
    'current': 'Listed, copy current',
    'stale': 'Listed, copy out of date',
}


def _iso(ts):
    if ts is None:
        return None
    if isinstance(ts, str):
        return ts
    try:
        return ts.isoformat()
    except Exception:
        return str(ts)


def _age_days(ts, now):
    """Days since ts, with `now` a naive UTC datetime. None if unreadable."""
    if ts is None:
        return None
    try:
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts.replace('Z', '+00:00'))
        if ts.tzinfo is not None:
            ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
        return max(0.0, (now - ts).total_seconds() / 86400.0)
    except Exception:
        return None


def _lane_state(last_at, now, idle_days):
    age = _age_days(last_at, now)
    if age is None:
        return 'idle', None
    return ('active' if age <= idle_days else 'idle'), round(age, 1)


def _read_directories(cur):
    cur.execute("SELECT probed_at, results FROM mcp_registry_probe_state "
                "WHERE id = 1")
    row = cur.fetchone()
    if not row:
        return None
    probed_at, results = row[0], row[1]
    if isinstance(results, str):
        results = json.loads(results)
    return probed_at, (results or {})


def real_outreach_status(conn_factory=None, now=None):
    """Read the three ledgers real outreach writes. Never raises; a ledger
    that cannot be read comes back as a lane with status 'unknown'.

    Public payload, so it carries no addresses and no self-submitted text:
    partner mail is named by lab (the targets are our own list), and a
    self-registration is named only once a human or the fit gate approved it.
    """
    now = now or datetime.utcnow()
    conn_factory = conn_factory or get_db
    events, lanes, summary = [], [], {}

    def _run(name, fn):
        conn = None
        try:
            conn = conn_factory()
            if conn is None:
                raise RuntimeError('no database connection')
            cur = conn.cursor()
            fn(cur)
        except Exception as e:
            logger.warning('real_outreach_status: %s unreadable: %s', name, e)
            lanes.append({'id': name, 'status': 'unknown',
                          'detail': 'ledger could not be read'})
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _directories(cur):
        got = _read_directories(cur)
        if got is None:
            lanes.append({'id': 'directories', 'name': 'Directory listings check',
                          'status': 'idle', 'last_activity': None,
                          'detail': 'no directory scan has been recorded'})
            return
        probed_at, results = got
        listed = sum(1 for r in results.values() if r.get('verdict') == 'present')
        stale = sum(1 for r in results.values()
                    if r.get('verdict') == 'present'
                    and (r.get('copy') or {}).get('state') == 'stale')
        missing = sum(1 for r in results.values()
                      if r.get('verdict') != 'present' and r.get('actionable', True))
        summary.update({'directories_tracked': len(results),
                        'directories_listed': listed,
                        'directories_stale_copy': stale,
                        'directories_missing': missing,
                        'directories_checked_at': _iso(probed_at)})
        for r in results.values():
            if r.get('verdict') == 'present':
                note = _DIRECTORY_COPY_NOTE.get((r.get('copy') or {}).get('state'),
                                                'Listed')
            elif not r.get('actionable', True):
                continue
            else:
                note = 'Not listed (' + str(r.get('verdict') or 'missing') + ')'
            events.append({'channel': 'directory', 'notes': note,
                           'platform': r.get('registry') or '',
                           'status': r.get('verdict'),
                           'timestamp': _iso(probed_at)})
        st, age = _lane_state(probed_at, now, IDLE_AFTER_DAYS['directories'])
        lanes.append({'id': 'directories', 'name': 'Directory listings check',
                      'status': st, 'last_activity': _iso(probed_at),
                      'age_days': age,
                      'detail': (f'{listed} of {len(results)} directories list '
                                 f'DC Hub; {stale} with out-of-date copy, '
                                 f'{missing} missing')})

    def _partner_email(cur):
        cur.execute(
            "SELECT COUNT(*) FILTER (WHERE status = 'sent'), "
            "       COUNT(*) FILTER (WHERE status = 'sent' AND sent_at > %s), "
            "       COUNT(*) FILTER (WHERE status = 'draft'), "
            "       MAX(sent_at) "
            "  FROM ai_lab_outreach_drafts", (now - timedelta(days=30),))
        sent_all, sent_30d, queued, last = cur.fetchone()
        summary.update({'partner_emails_sent': sent_all or 0,
                        'partner_emails_sent_30d': sent_30d or 0,
                        'partner_drafts_queued': queued or 0,
                        'partner_email_last_sent_at': _iso(last)})
        cur.execute(
            "SELECT target_slug, sent_at, delivery_state "
            "  FROM ai_lab_outreach_drafts WHERE status = 'sent' "
            " ORDER BY sent_at DESC NULLS LAST LIMIT 10")
        names = {}
        try:
            from routes.ai_lab_outreach import _TARGETS
            names = {t['slug']: t['name'] for t in _TARGETS}
        except Exception:
            pass
        for slug, sent_at, delivery in cur.fetchall():
            events.append({'channel': 'email',
                           'notes': 'Partner email ' + (delivery or 'submitted'),
                           'platform': names.get(slug, 'AI lab'),
                           'status': delivery or 'submitted',
                           'timestamp': _iso(sent_at)})
        st, age = _lane_state(last, now, IDLE_AFTER_DAYS['partner_email'])
        detail = (f'{sent_30d or 0} sent in 30d; {queued or 0} drafts queued')
        if not queued:
            detail += ' - the daily sender has nothing to send'
        lanes.append({'id': 'partner_email', 'name': 'Partner outreach (email)',
                      'status': st, 'last_activity': _iso(last),
                      'age_days': age, 'detail': detail})

    def _self_registration(cur):
        cur.execute(
            "SELECT COUNT(*), COUNT(*) FILTER (WHERE submitted_at > %s), "
            "       COUNT(*) FILTER (WHERE status IN ('auto_approved','approved')), "
            "       MAX(submitted_at) "
            "  FROM ai_platform_submissions", (now - timedelta(days=30),))
        total, last30, approved, last = cur.fetchone()
        summary.update({'self_registrations': total or 0,
                        'self_registrations_30d': last30 or 0,
                        'self_registrations_approved': approved or 0,
                        'self_registration_last_at': _iso(last)})
        cur.execute(
            "SELECT name, COALESCE(approved_at, processed_at, submitted_at) "
            "  FROM ai_platform_submissions "
            " WHERE status IN ('auto_approved','approved') "
            " ORDER BY 2 DESC NULLS LAST LIMIT 5")
        for name, at in cur.fetchall():
            events.append({'channel': 'self_registration',
                           'notes': 'Agent platform onboarded',
                           'platform': (name or '')[:80],
                           'status': 'approved', 'timestamp': _iso(at)})
        st, age = _lane_state(last, now, IDLE_AFTER_DAYS['self_registration'])
        lanes.append({'id': 'self_registration',
                      'name': 'Agent self-registration',
                      'status': st, 'last_activity': _iso(last), 'age_days': age,
                      'detail': (f'{last30 or 0} submissions in 30d; '
                                 f'{approved or 0} approved all-time')})

    _run('directories', _directories)
    _run('partner_email', _partner_email)
    _run('self_registration', _self_registration)
    events.sort(key=lambda e: e.get('timestamp') or '', reverse=True)
    return {'events': events[:10], 'lanes': lanes, 'summary': summary}


_REAL_CACHE = {'at': 0.0, 'val': None}
_REAL_CACHE_TTL_S = 60


def _cached_real_outreach():
    """/api/ai-ecosystem/status is read by four public pages; the ledgers move
    on a daily cadence, so a minute of reuse costs nothing and keeps a page
    view from opening three pool connections."""
    t = time.time()
    if _REAL_CACHE['val'] is not None and t - _REAL_CACHE['at'] < _REAL_CACHE_TTL_S:
        return _REAL_CACHE['val']
    val = real_outreach_status()
    _REAL_CACHE.update(at=t, val=val)
    return val


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
    if not is_valid_internal_key(request.headers.get("X-Internal-Key") or request.headers.get("X-Admin-Key")):
        return jsonify({'error': 'unauthorized'}), 401
    results = agent.run_cycle()
    return jsonify({
        'success': True,
        'results': results
    })

@ai_ecosystem_bp.route('/api/ai-ecosystem/start', methods=['POST'])
def start_agent():
    """Start the automated scheduler"""
    if not is_valid_internal_key(request.headers.get("X-Internal-Key") or request.headers.get("X-Admin-Key")):
        return jsonify({'error': 'unauthorized'}), 401
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
    if not is_valid_internal_key(request.headers.get("X-Internal-Key") or request.headers.get("X-Admin-Key")):
        return jsonify({'error': 'unauthorized'}), 401
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
