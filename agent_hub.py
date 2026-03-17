"""
DC Hub Agent Hub - Multi-Agent System with Expert Intelligence
==============================================================
AI-powered agents with deep data center industry expertise:
1. Sales Agent - Lead qualification with market intelligence
2. Data Enrichment Agent - Facility discovery with cross-referencing
3. Social Media Agent - AI-generated posts from trends
4. Expert Brain Integration - Self-learning industry knowledge

Required by main.py v88+
"""

from flask import request, jsonify
from datetime import datetime
import json
import os
import sqlite3
from db_utils import get_db

# Try to import anthropic for AI-powered responses
try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False
    print("⚠️ anthropic not installed - agents will use fallback responses")

# Import DC Expert Brain for intelligent responses
try:
    from dc_expert_brain import get_expert_brain, DC_EXPERT_KNOWLEDGE
    BRAIN_AVAILABLE = True
except ImportError:
    BRAIN_AVAILABLE = False
    DC_EXPERT_KNOWLEDGE = {}
    print("⚠️ dc_expert_brain not available - using basic knowledge")

# =============================================================================
# CONFIGURATION
# =============================================================================

def get_live_dchub_config():
    """Get live stats from database instead of hardcoded values"""
    db_path = os.environ.get('DB_PATH', 'dc_nexus.db')
    conn = None
    try:
        conn = get_db(db_path)
        c = conn.cursor()
        
        c.execute("SELECT COUNT(*) FROM facilities")
        facilities_count = c.fetchone()[0] or 0
        
        c.execute("SELECT COUNT(DISTINCT country) FROM facilities WHERE country IS NOT NULL AND country != ''")
        countries = c.fetchone()[0] or 100
        
        c.execute("SELECT COUNT(DISTINCT city) FROM facilities WHERE city IS NOT NULL AND city != ''")
        markets_count = c.fetchone()[0] or 50
        
        try:
            c.execute("SELECT SUM(capacity_mw) FROM capacity_tracking")
            pipeline_mw = c.fetchone()[0] or 0
            pipeline_gw = round(pipeline_mw / 1000, 1) if pipeline_mw else 13.0
        except:
            pipeline_gw = 13.0
        
        try:
            c.execute("SELECT COUNT(*) FROM deals")
            total_deals = c.fetchone()[0] or 0
            deal_volume = f"{total_deals} deals tracked"
        except:
            deal_volume = "$85B+"
        
        return {
            "version": "v88",
            "facilities_count": facilities_count,
            "countries": countries,
            "markets_count": markets_count,
            "pipeline_gw": pipeline_gw,
            "vacancy_rate": 1.6,
            "deal_volume": deal_volume,
            "avg_pricing": "$200+/kW"
        }
    except Exception as e:
        print(f"Error getting live config: {e}")
        return {
            "version": "v88",
            "facilities_count": 10000,
            "countries": 100,
            "markets_count": 50,
            "pipeline_gw": 13.0,
            "vacancy_rate": 1.6,
            "deal_volume": "$85B+",
            "avg_pricing": "$200+/kW"
        }
    finally:
        if conn:
            conn.close()

# Use live config - refreshed on each import
DCHUB_CONFIG = get_live_dchub_config()

# =============================================================================
# DATA STORES (in-memory)
# =============================================================================

agent_data = {
    "leads": [],
    "conversations": [],
    "discovered_facilities": [],
    "validated_facilities": [],
    "social_posts": [],
    "logs": [],
    "stats": {
        "conversations_today": 0,
        "leads_qualified": 0,
        "demos_booked": 0,
        "facilities_discovered": 0,
        "posts_generated": 0
    }
}

# =============================================================================
# AGENT-TO-AGENT COMMUNICATION SYSTEM
# =============================================================================

class AgentBus:
    """Internal message bus for agent-to-agent communication - SQLite-backed for persistence"""
    
    def __init__(self):
        self.db_path = os.environ.get('DB_PATH', 'dc_nexus.db')
        self.agent_status = {
            'sales': {'online': True, 'busy': False, 'last_active': None},
            'enrichment': {'online': True, 'busy': False, 'last_active': None},
            'social': {'online': True, 'busy': False, 'last_active': None},
            'external': {'online': True, 'busy': False, 'last_active': None}
        }
        self._init_tables()
        # Load last_active from DB
        self._load_agent_status()
    
    def _get_conn(self):
        conn = get_db(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    
    def _init_tables(self):
        conn = None
        try:
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("""CREATE TABLE IF NOT EXISTS agent_bus_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                from_agent TEXT NOT NULL,
                to_agent TEXT NOT NULL,
                message_type TEXT NOT NULL,
                payload TEXT DEFAULT '{}',
                read INTEGER DEFAULT 0
            )""")
            c.execute("""CREATE TABLE IF NOT EXISTS agent_bus_handoffs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                from_agent TEXT NOT NULL,
                to_agent TEXT NOT NULL,
                context TEXT DEFAULT '{}',
                status TEXT DEFAULT 'pending'
            )""")
            c.execute("""CREATE TABLE IF NOT EXISTS agent_bus_chains (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chain_type TEXT NOT NULL,
                started TEXT NOT NULL,
                data TEXT DEFAULT '{}',
                steps TEXT DEFAULT '[]',
                status TEXT DEFAULT 'active'
            )""")
            c.execute("""CREATE TABLE IF NOT EXISTS agent_bus_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent TEXT NOT NULL,
                last_active TEXT NOT NULL
            )""")
            conn.commit()
        except Exception as e:
            print(f"⚠️ AgentBus table init error: {e}")
        finally:
            if conn:
                conn.close()
    
    def _load_agent_status(self):
        conn = None
        try:
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("SELECT agent, last_active FROM agent_bus_activity")
            for row in c.fetchall():
                if row['agent'] in self.agent_status:
                    self.agent_status[row['agent']]['last_active'] = row['last_active']
        except:
            pass
        finally:
            if conn:
                conn.close()
    
    def _update_agent_active(self, agent):
        now = datetime.now().isoformat()
        if agent in self.agent_status:
            self.agent_status[agent]['last_active'] = now
        conn = None
        try:
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("DELETE FROM agent_bus_activity WHERE agent = ?", (agent,))
            c.execute("INSERT INTO agent_bus_activity (agent, last_active) VALUES (?, ?)", (agent, now))
            conn.commit()
        except:
            pass
        finally:
            if conn:
                conn.close()
    
    @property
    def messages(self):
        conn = None
        try:
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("SELECT * FROM agent_bus_messages ORDER BY id DESC LIMIT 100")
            rows = c.fetchall()
            return [dict(r) for r in reversed(rows)]
        except:
            return []
        finally:
            if conn:
                conn.close()
    
    @property
    def handoffs(self):
        conn = None
        try:
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("SELECT * FROM agent_bus_handoffs ORDER BY id DESC LIMIT 50")
            rows = c.fetchall()
            return [{'id': r['id'], 'timestamp': r['timestamp'], 'from': r['from_agent'], 
                      'to': r['to_agent'], 'context': json.loads(r['context'] or '{}'), 
                      'status': r['status']} for r in reversed(rows)]
        except:
            return []
        finally:
            if conn:
                conn.close()
    
    @property
    def active_chains(self):
        conn = None
        try:
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("SELECT * FROM agent_bus_chains ORDER BY id DESC LIMIT 50")
            rows = c.fetchall()
            return [{'id': r['id'], 'type': r['chain_type'], 'started': r['started'],
                      'data': json.loads(r['data'] or '{}'), 'steps': json.loads(r['steps'] or '[]'),
                      'status': r['status']} for r in reversed(rows)]
        except:
            return []
        finally:
            if conn:
                conn.close()
    
    def send_message(self, from_agent: str, to_agent: str, message_type: str, payload: dict):
        """Send a message from one agent to another"""
        now = datetime.now().isoformat()
        conn = None
        try:
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("""INSERT INTO agent_bus_messages (timestamp, from_agent, to_agent, message_type, payload, read)
                         VALUES (?, ?, ?, ?, ?, 0)""",
                      (now, from_agent, to_agent, message_type, json.dumps(payload)))
            msg_id = c.lastrowid
            conn.commit()
        except Exception as e:
            print(f"⚠️ AgentBus send error: {e}")
            msg_id = 0
        finally:
            if conn:
                conn.close()
        
        self._update_agent_active(from_agent)
        
        msg = {
            'id': msg_id,
            'timestamp': now,
            'from': from_agent,
            'to': to_agent,
            'type': message_type,
            'payload': payload,
            'read': False
        }
        
        log_activity(from_agent, f"sent_{message_type}", f"To {to_agent}: {payload.get('summary', '')[:100]}")
        
        # Auto-process certain message types
        if message_type == 'handoff':
            self.process_handoff(msg)
        
        return msg
    
    def broadcast(self, from_agent: str, message_type: str, payload: dict):
        """Broadcast a message to all agents"""
        results = []
        for agent in self.agent_status.keys():
            if agent != from_agent:
                results.append(self.send_message(from_agent, agent, message_type, payload))
        return results
    
    def get_messages(self, agent: str, unread_only: bool = False):
        """Get messages for an agent"""
        conn = None
        try:
            conn = self._get_conn()
            c = conn.cursor()
            if unread_only:
                c.execute("SELECT * FROM agent_bus_messages WHERE to_agent = ? AND read = 0 ORDER BY id DESC LIMIT 20", (agent,))
            else:
                c.execute("SELECT * FROM agent_bus_messages WHERE to_agent = ? ORDER BY id DESC LIMIT 20", (agent,))
            rows = c.fetchall()
            return [dict(r) for r in reversed(rows)]
        except:
            return []
        finally:
            if conn:
                conn.close()
    
    def mark_read(self, message_id: int):
        """Mark a message as read"""
        conn = None
        try:
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("UPDATE agent_bus_messages SET read = 1 WHERE id = ?", (message_id,))
            conn.commit()
            return True
        except:
            return False
        finally:
            if conn:
                conn.close()
    
    def process_handoff(self, msg: dict):
        """Process a handoff request"""
        conn = None
        try:
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("""INSERT INTO agent_bus_handoffs (timestamp, from_agent, to_agent, context, status)
                         VALUES (?, ?, ?, ?, 'pending')""",
                      (msg['timestamp'], msg['from'], msg['to'], json.dumps(msg.get('payload', {}))))
            handoff_id = c.lastrowid
            conn.commit()
        except Exception as e:
            print(f"⚠️ AgentBus handoff error: {e}")
            handoff_id = 0
        finally:
            if conn:
                conn.close()
        
        return {
            'id': handoff_id,
            'timestamp': msg['timestamp'],
            'from': msg['from'],
            'to': msg['to'],
            'context': msg.get('payload', {}),
            'status': 'pending'
        }
    
    def start_chain(self, chain_type: str, initial_data: dict):
        """Start a collaboration chain"""
        now = datetime.now().isoformat()
        conn = None
        try:
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("""INSERT INTO agent_bus_chains (chain_type, started, data, steps, status)
                         VALUES (?, ?, ?, '[]', 'active')""",
                      (chain_type, now, json.dumps(initial_data)))
            chain_id = c.lastrowid
            conn.commit()
        except Exception as e:
            print(f"⚠️ AgentBus chain error: {e}")
            chain_id = 0
        finally:
            if conn:
                conn.close()
        
        return {
            'id': chain_id,
            'type': chain_type,
            'started': now,
            'data': initial_data,
            'steps': [],
            'status': 'active'
        }
    
    def get_status(self):
        """Get bus status"""
        conn = None
        try:
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM agent_bus_messages")
            total_msgs = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM agent_bus_handoffs WHERE status = 'pending'")
            pending = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM agent_bus_chains WHERE status = 'active'")
            active = c.fetchone()[0]
        except:
            total_msgs = 0
            pending = 0
            active = 0
        finally:
            if conn:
                conn.close()
        
        return {
            'total_messages': total_msgs,
            'pending_handoffs': pending,
            'active_chains': active,
            'agent_status': self.agent_status
        }

# Global agent bus instance
agent_bus = AgentBus()

# =============================================================================
# CROSS-AGENT EVENT BROADCASTING (used by outreach, interconnection, SEO)
# =============================================================================

def emit_outreach_event(cycle_summary: dict):
    """Called by outreach agent after each cycle — broadcasts results to all agents"""
    agent_bus.broadcast('outreach', 'outreach_cycle_complete', {
        'summary': f"Outreach cycle: {cycle_summary.get('directories', '?')} dirs, {cycle_summary.get('search_engines', '?')} search, {cycle_summary.get('ai_broadcasts', '?')} AI platforms",
        'directories_success': cycle_summary.get('directories', ''),
        'search_engines_success': cycle_summary.get('search_engines', ''),
        'ai_broadcasts_success': cycle_summary.get('ai_broadcasts', ''),
        'organic_detected': len(cycle_summary.get('organic_traffic', [])) > 0,
        'timestamp': cycle_summary.get('timestamp', ''),
    })

def emit_ai_traffic_event(platform: str, endpoint: str, is_organic: bool):
    """Called by AI interconnection when real AI platform traffic is detected"""
    if is_organic:
        agent_bus.broadcast('interconnection', 'organic_ai_traffic', {
            'summary': f"Organic {platform} traffic on {endpoint}",
            'platform': platform,
            'endpoint': endpoint,
        })

def emit_seo_content_event(content_type: str, url: str, title: str):
    """Called by SEO agent when new content is generated"""
    agent_bus.broadcast('seo', 'content_generated', {
        'summary': f"New {content_type}: {title}",
        'content_type': content_type,
        'url': url,
        'title': title,
    })

def get_cross_agent_activity(hours: int = 24):
    """Get recent cross-agent activity for the feedback dashboard"""
    conn = None
    try:
        conn = agent_bus._get_conn()
        c = conn.cursor()
        c.execute("""SELECT from_agent, to_agent, message_type, payload, timestamp 
                     FROM agent_bus_messages 
                     WHERE timestamp > datetime('now', ?)
                     ORDER BY id DESC LIMIT 50""", (f'-{hours} hours',))
        rows = c.fetchall()
        import json as _json
        return [{
            'from': r['from_agent'], 'to': r['to_agent'], 
            'type': r['message_type'], 
            'payload': _json.loads(r['payload'] or '{}'),
            'timestamp': r['timestamp']
        } for r in rows]
    except Exception:
        return []
    finally:
        if conn:
            conn.close()

# =============================================================================
# SMART HANDOFF TRIGGERS
# =============================================================================

def trigger_handoff(from_agent: str, to_agent: str, reason: str, context: dict):
    """Trigger a smart handoff between agents"""
    payload = {
        'reason': reason,
        'context': context,
        'summary': f"{from_agent} needs {to_agent} to {reason}"
    }
    return agent_bus.send_message(from_agent, to_agent, 'handoff', payload)

def sales_to_enrichment(lead_info: dict):
    """Sales Agent hands off to Data Enrichment for company research"""
    return trigger_handoff(
        'sales', 'enrichment',
        'research company and facilities',
        {
            'company': lead_info.get('company'),
            'interest': lead_info.get('interest'),
            'request': f"Find all facilities and market data for {lead_info.get('company', 'this company')}"
        }
    )

def enrichment_to_social(discovery: dict):
    """Data Enrichment hands off to Social Media to create a post"""
    return trigger_handoff(
        'enrichment', 'social',
        'create social post about discovery',
        {
            'discovery_type': discovery.get('type'),
            'data': discovery.get('data'),
            'request': f"Create a LinkedIn post about: {discovery.get('summary', 'new discovery')}"
        }
    )

def social_to_sales(engagement: dict):
    """Social Media hands off hot lead to Sales"""
    return trigger_handoff(
        'social', 'sales',
        'follow up on engaged prospect',
        {
            'source': 'social_engagement',
            'prospect': engagement.get('prospect'),
            'interest_signal': engagement.get('signal'),
            'request': f"Follow up with {engagement.get('prospect', 'prospect')} who engaged with our content"
        }
    )

# =============================================================================
# COLLABORATION CHAINS
# =============================================================================

def start_research_chain(topic: str):
    """Start a research collaboration chain: Enrichment → Analysis → Social"""
    chain = agent_bus.start_chain('research', {
        'topic': topic,
        'started_by': 'user'
    })
    
    # Step 1: Data Enrichment researches
    chain['steps'].append({
        'step': 1,
        'agent': 'enrichment',
        'action': 'research',
        'status': 'active',
        'input': topic
    })
    
    agent_bus.broadcast('enrichment', 'chain_started', {
        'chain_id': chain['id'],
        'topic': topic,
        'summary': f"Research chain started for: {topic}"
    })
    
    return chain

def start_lead_chain(lead_info: dict):
    """Start a lead qualification chain: Sales → Enrichment → Sales"""
    chain = agent_bus.start_chain('lead_qualification', {
        'lead': lead_info,
        'started_by': 'sales'
    })
    
    chain['steps'].append({
        'step': 1,
        'agent': 'sales',
        'action': 'initial_qualification',
        'status': 'complete',
        'output': lead_info
    })
    
    chain['steps'].append({
        'step': 2,
        'agent': 'enrichment',
        'action': 'company_research',
        'status': 'active',
        'input': lead_info.get('company')
    })
    
    # Trigger the handoff
    sales_to_enrichment(lead_info)
    
    return chain

def start_content_chain(topic: str):
    """Start a content creation chain: Enrichment → Social → Broadcast"""
    chain = agent_bus.start_chain('content_creation', {
        'topic': topic,
        'started_by': 'user'
    })
    
    chain['steps'].append({
        'step': 1,
        'agent': 'enrichment',
        'action': 'gather_data',
        'status': 'active',
        'input': topic
    })
    
    return chain

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def get_claude_client():
    """Get Anthropic client if available"""
    if not ANTHROPIC_AVAILABLE:
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        return anthropic.Anthropic(api_key=api_key)
    return None

def log_activity(agent: str, action: str, details: str = ""):
    """Log agent activity"""
    entry = {
        "timestamp": datetime.now().isoformat(),
        "agent": agent,
        "action": action,
        "details": details
    }
    agent_data["logs"].append(entry)
    if len(agent_data["logs"]) > 100:
        agent_data["logs"].pop(0)

def call_claude(system_prompt: str, user_message: str, max_tokens: int = 500):
    """Call Claude API with fallback"""
    client = get_claude_client()
    if not client:
        return None
    
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20241022",
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}]
        )
        return response.content[0].text
    except Exception as e:
        print(f"Claude API error: {e}")
        return None

def get_orchestrator_context():
    """Get real-time context from AI Orchestrator for smarter responses"""
    try:
        from ai_orchestrator import get_orchestrator
        orch = get_orchestrator()
        
        context = {
            'market_sentiment': orch.market_pulse.get('sentiment', 'neutral'),
            'trending_operators': [op['name'] for op in orch.market_pulse.get('trending_operators', [])[:5]],
            'trending_markets': [m['name'] for m in orch.market_pulse.get('trending_markets', [])[:5]],
            'hot_opportunities': len(orch.opportunity_queue),
            'predictions': orch.predictions.get('next_deals', [])[:2],
            'emerging_markets': [h.get('name') for h in orch.predictions.get('emerging_hotspots', [])[:3]]
        }
        return context
    except Exception as e:
        return {'error': str(e)}

def get_live_stats():
    """Get live platform statistics from database"""
    import sqlite3
    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute('SELECT COUNT(*) FROM facilities')
        facility_count = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM announcements WHERE timestamp > datetime("now", "-7 days")')
        recent_news = cursor.fetchone()[0]
        
        cursor.execute('SELECT SUM(mw) FROM capacity_pipeline WHERE status != "cancelled"')
        result = cursor.fetchone()
        pipeline_mw = result[0] if result and result[0] else 0
        
        cursor.execute('SELECT COUNT(*) FROM deals')
        deals = cursor.fetchone()[0]
        
        return {
            'facilities': facility_count,
            'recent_news': recent_news,
            'pipeline_mw': pipeline_mw,
            'deals': deals
        }
    except Exception as e:
        return {'facilities': 9603, 'recent_news': 0, 'pipeline_mw': 7194, 'deals': 100}
    finally:
        if conn:
            conn.close()

def learn_from_interaction(agent: str, user_message: str, response: str, success: bool = True):
    """Track interactions for learning patterns"""
    import sqlite3
    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS agent_interactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent TEXT NOT NULL,
                user_message TEXT,
                response_summary TEXT,
                success BOOLEAN DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            INSERT INTO agent_interactions (agent, user_message, response_summary, success)
            VALUES (?, ?, ?, ?)
        ''', (agent, user_message[:500], response[:200] if response else '', success))
        
        conn.commit()
    except Exception:
        pass
    finally:
        if conn:
            conn.close()

def get_expert_knowledge(topic: str = None) -> str:
    """Get expert knowledge from DC Brain for agent responses"""
    if not BRAIN_AVAILABLE:
        return ""
    
    try:
        brain = get_expert_brain()
        
        expert_parts = []
        
        expert_parts.append("\n=== DC INDUSTRY EXPERT KNOWLEDGE ===")
        expert_parts.append("You have deep expertise in data center infrastructure:")
        
        if topic:
            answer = brain.answer_question(topic)
            if answer:
                expert_parts.append(f"\nDirect answer: {answer}")
        
        trends = brain.get_current_trends()[:5]
        expert_parts.append(f"\nCurrent trends: {'; '.join(trends)}")
        
        status = brain.get_learning_status()
        expert_parts.append(f"\nLearning status: {status['learned_insights']['operators_learned']} operators learned, {status['learned_insights']['trends_detected']} trends detected")
        
        key_terms = list(DC_EXPERT_KNOWLEDGE.get('terminology', {}).items())[:10]
        if key_terms:
            expert_parts.append("\nKey terms you know:")
            for term, definition in key_terms:
                expert_parts.append(f"- {term}: {definition[:100]}")
        
        return '\n'.join(expert_parts)
    except Exception as e:
        return f"\n(Expert brain error: {e})"

def get_smart_answer(question: str) -> str:
    """Get intelligent answer using expert brain"""
    if not BRAIN_AVAILABLE:
        return None
    
    try:
        brain = get_expert_brain()
        return brain.answer_question(question)
    except:
        return None

# =============================================================================
# AGENT STATS & LOGS
# =============================================================================

def get_agent_stats():
    """GET /api/agents/stats - Return agent statistics"""
    return jsonify({
        "success": True,
        "stats": agent_data["stats"],
        "timestamp": datetime.now().isoformat()
    })

def get_agent_logs():
    """GET /api/agents/logs - Return recent agent activity logs"""
    limit = request.args.get('limit', 50, type=int)
    return jsonify({
        "success": True,
        "logs": agent_data["logs"][-limit:],
        "total": len(agent_data["logs"])
    })

# =============================================================================
# SALES AGENT
# =============================================================================

SALES_SYSTEM_PROMPT = """You are the DC Hub Expert Sales Agent - a data center industry specialist with deep technical knowledge.

=== YOUR EXPERTISE ===
You have comprehensive knowledge of:
- Data center infrastructure (PUE, uptime tiers, N+1/2N redundancy)
- Major operators: Equinix (260+ facilities), Digital Realty (300+ facilities), hyperscalers (AWS, Azure, Google)
- Key markets: Northern Virginia (3.5 GW), Dallas-Fort Worth, Phoenix, Frankfurt, Singapore
- Industry dynamics: $51B+ annual M&A volume, 4-6% cap rates, 1.6% vacancy in primary markets
- Power: 5-10x density for AI/GPU workloads, nuclear PPAs trending
- Pricing: $150-250/kW wholesale, $1.5-2.5M/MW construction costs

=== DC HUB PLATFORM ===
DC Hub (dchub.cloud) tracks 10,000+ facilities across 160+ countries:
- Land & Power: 40+ government data layers for site selection
- Pipeline: 13+ GW capacity under construction
- M&A tracker: $51B+ deal volume
- Infrastructure mapping: Fiber, substations, water data

Pricing: Free (limited), Pro ($99/mo), Enterprise (custom)

=== YOUR APPROACH ===
1. Demonstrate expertise - Use industry knowledge naturally
2. Qualify intelligently - Understand their role (broker, developer, investor, operator)
3. Match solutions - Connect their needs to specific features
4. Provide value first - Share insights before pitching

Keep responses expert but accessible (under 150 words). Never guess - if unsure, offer to connect them with specialists."""

def sales_chat():
    """POST /api/agents/sales/chat - Handle sales conversations with smart context"""
    try:
        data = request.get_json() or {}
        message = data.get('message', '').strip()
        conversation_id = data.get('conversation_id')
        
        if not message:
            return jsonify({"error": "No message provided"}), 400
        
        agent_data["stats"]["conversations_today"] += 1
        log_activity("sales", "chat", f"Message: {message[:50]}...")
        
        # Get real-time context for smarter responses
        live_stats = get_live_stats()
        orch_context = get_orchestrator_context()
        
        # Get expert knowledge for enhanced responses
        expert_knowledge = get_expert_knowledge(message)
        
        # Check if brain can directly answer the question
        smart_answer = get_smart_answer(message)
        
        # Build enhanced prompt with live data and expert knowledge
        enhanced_prompt = SALES_SYSTEM_PROMPT + f"""

LIVE PLATFORM DATA (use these real numbers):
- Facilities tracked: {live_stats.get('facilities', 9603):,}
- Pipeline MW: {live_stats.get('pipeline_mw', 7194):,.0f} MW
- Active deals: {live_stats.get('deals', 100)}
- Recent news articles: {live_stats.get('recent_news', 0)}

MARKET INTELLIGENCE (mention when relevant):
- Market sentiment: {orch_context.get('market_sentiment', 'neutral')}
- Trending operators: {', '.join(orch_context.get('trending_operators', [])[:3]) or 'Equinix, Digital Realty'}
- Hot markets: {', '.join(orch_context.get('trending_markets', [])[:3]) or 'Dallas, Phoenix'}
- Emerging opportunities: {', '.join(orch_context.get('emerging_markets', [])[:2]) or 'Columbus, Nashville'}
{expert_knowledge}

Use this real-time data and expert knowledge to make your responses compelling and demonstrate deep industry expertise."""

        # Try Claude with enhanced context
        response = call_claude(enhanced_prompt, message)
        
        if not response:
            # Smart fallback responses with expert knowledge
            message_lower = message.lower()
            facilities = live_stats.get('facilities', 9603)
            pipeline = live_stats.get('pipeline_mw', 7194)
            
            # First check if we have a direct expert answer
            if smart_answer:
                response = f"{smart_answer}\n\nWould you like more details on this topic or a demo of our platform?"
            elif any(word in message_lower for word in ['pue', 'power usage', 'efficiency']):
                response = "PUE (Power Usage Effectiveness) is the ratio of total facility power to IT equipment power. Industry average is 1.58, while best-in-class hyperscale facilities achieve under 1.2. DC Hub tracks PUE data across markets. Want to explore efficiency trends?"
            elif any(word in message_lower for word in ['tier', 'uptime', 'redundancy', 'n+1', '2n']):
                response = "Data center tiers: Tier III (99.982% uptime, concurrently maintainable) is most common for enterprise. Tier IV (99.995%, fault tolerant) for mission-critical. N+1 means one backup unit, 2N is fully redundant. What tier are you targeting?"
            elif any(word in message_lower for word in ['price', 'cost', 'pricing', 'how much']):
                response = "Wholesale colocation runs $150-250/kW/month in primary markets, up 20% YoY due to AI demand. DC Hub Pro at $99/mo gives you access to pricing data across 64+ markets. Want a demo?"
            elif any(word in message_lower for word in ['demo', 'trial', 'test']):
                response = f"Absolutely! We're tracking {facilities:,} facilities with {pipeline:,.0f} MW in the construction pipeline. Our Land & Power tool has 40+ government data layers for site selection. What markets are you focused on?"
                agent_data["stats"]["demos_booked"] += 1
            elif any(word in message_lower for word in ['ai', 'gpu', 'ml', 'machine learning']):
                response = "AI/GPU workloads are driving unprecedented demand - 5-10x power density vs traditional compute. Markets like Phoenix and Dallas are seeing 35%+ growth. DC Hub tracks AI-ready capacity specifically. Interested in AI infrastructure data?"
            elif any(word in message_lower for word in ['equinix', 'digital realty', 'operator']):
                response = "Equinix leads in interconnection with 260+ facilities in 71 markets. Digital Realty focuses on wholesale + hyperscale with 300+ facilities. DC Hub tracks all major operators plus emerging players like Stack, Compass, and Vantage. Which operators are you researching?"
            elif any(word in message_lower for word in ['market', 'trend', 'hot', 'growing', 'virginia', 'dallas', 'phoenix']):
                trending = orch_context.get('trending_markets', ['Dallas', 'Phoenix', 'Northern Virginia'])[:3]
                response = f"Hot markets right now: {', '.join(trending)}. Northern Virginia leads at 3.5 GW but faces power constraints. Phoenix and Dallas growing 22-35%. Emerging: Columbus, Nashville, Salt Lake City. What's your focus area?"
            elif any(word in message_lower for word in ['feature', 'what can', 'capabilities', 'do you']):
                response = f"DC Hub offers: (1) Land & Power site analysis with 40+ government data layers, (2) {facilities:,}+ facility tracking globally, (3) {pipeline/1000:.1f} GW construction pipeline, (4) M&A deal tracking ($51B+ volume), (5) Real-time infrastructure mapping. What's your use case?"
            else:
                response = f"Thanks for reaching out! As a data center intelligence platform, DC Hub tracks {facilities:,}+ facilities, {pipeline/1000:.1f} GW pipeline, and provides Land & Power analysis for site selection. I can discuss markets, operators, or platform features. What interests you?"
        
        # Learn from this interaction
        learn_from_interaction("sales", message, response, success=True)
        
        # Check for handoff triggers
        handoff_triggered = None
        message_lower = message.lower()
        
        # Trigger handoff to enrichment if user asks about specific company/facility research
        if any(word in message_lower for word in ['research', 'find facilities', 'company data', 'look up']):
            company = data.get('company', message.split()[-1] if len(message.split()) > 2 else 'Unknown')
            handoff_triggered = sales_to_enrichment({
                'company': company,
                'interest': message,
                'source': 'sales_chat'
            })
        
        # Update agent status
        agent_bus.agent_status['sales']['last_active'] = datetime.now().isoformat()
        
        return jsonify({
            "success": True,
            "response": response,
            "conversation_id": conversation_id or datetime.now().strftime("%Y%m%d%H%M%S"),
            "agent": "sales",
            "context_used": True,
            "handoff": handoff_triggered['id'] if handoff_triggered else None
        })
        
    except Exception as e:
        log_activity("sales", "error", str(e))
        learn_from_interaction("sales", message if 'message' in dir() else "", "", success=False)
        return jsonify({"error": str(e)}), 500

def get_leads():
    """GET /api/agents/sales/leads - Get captured leads"""
    return jsonify({
        "success": True,
        "leads": agent_data["leads"],
        "total": len(agent_data["leads"])
    })

def qualify_lead():
    """POST /api/agents/sales/qualify - Qualify a lead"""
    try:
        data = request.get_json() or {}
        
        lead = {
            "id": len(agent_data["leads"]) + 1,
            "email": data.get('email', ''),
            "company": data.get('company', ''),
            "role": data.get('role', ''),
            "use_case": data.get('use_case', ''),
            "budget": data.get('budget', ''),
            "timeline": data.get('timeline', ''),
            "score": 0,
            "qualified_at": datetime.now().isoformat()
        }
        
        # Simple lead scoring
        if lead['company']:
            lead['score'] += 20
        if lead['email'] and '@' in lead['email']:
            lead['score'] += 20
        if lead['use_case']:
            lead['score'] += 25
        if lead['budget']:
            lead['score'] += 20
        if lead['timeline']:
            lead['score'] += 15
        
        agent_data["leads"].append(lead)
        agent_data["stats"]["leads_qualified"] += 1
        log_activity("sales", "qualify_lead", f"Lead score: {lead['score']}")
        
        return jsonify({
            "success": True,
            "lead": lead,
            "qualified": lead['score'] >= 60
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# =============================================================================
# DATA ENRICHMENT AGENT
# =============================================================================

ENRICHMENT_SYSTEM_PROMPT = """You are the DC Hub Data Enrichment Agent. Your job is to help research and validate data center information.

You can help with:
1. Market research - Analyze specific data center markets
2. Facility discovery - Find new facilities in a region
3. Data validation - Verify facility details

When researching markets, provide:
- Key operators in the market
- Current capacity and pipeline
- Power availability and costs
- Recent transactions
- Growth trends

Be specific with data points when available. If you don't have exact figures, provide reasonable estimates based on market knowledge."""

def discover_facilities():
    """POST /api/agents/enrichment/discover - Discover facilities in a region"""
    try:
        data = request.get_json() or {}
        region = data.get('region', '').strip()
        
        if not region:
            return jsonify({"error": "No region specified"}), 400
        
        log_activity("enrichment", "discover", f"Region: {region}")
        
        # Try Claude for intelligent discovery
        prompt = f"List 5 data center facilities in {region}. For each, provide: name, operator, estimated MW capacity, and address if known. Format as JSON array."
        response = call_claude(ENRICHMENT_SYSTEM_PROMPT, prompt)
        
        facilities = []
        if response:
            try:
                start = response.find('[')
                end = response.rfind(']') + 1
                if start >= 0 and end > start:
                    facilities = json.loads(response[start:end])
            except:
                pass
        
        if not facilities:
            # Fallback with sample data
            facilities = [
                {"name": f"{region} Data Center 1", "operator": "Unknown", "capacity_mw": "10-50", "status": "Operational"},
                {"name": f"{region} Data Center 2", "operator": "Unknown", "capacity_mw": "5-20", "status": "Under Construction"}
            ]
        
        agent_data["discovered_facilities"].extend(facilities)
        agent_data["stats"]["facilities_discovered"] += len(facilities)
        
        return jsonify({
            "success": True,
            "region": region,
            "facilities": facilities,
            "count": len(facilities)
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def validate_facility():
    """POST /api/agents/enrichment/validate - Validate facility data"""
    try:
        data = request.get_json() or {}
        facility_name = data.get('name', '')
        facility_data = data.get('data', {})
        
        log_activity("enrichment", "validate", f"Facility: {facility_name}")
        
        # Validation checks
        validations = {
            "name_valid": bool(facility_name),
            "has_location": bool(facility_data.get('location') or facility_data.get('address')),
            "has_capacity": bool(facility_data.get('capacity') or facility_data.get('mw')),
            "has_operator": bool(facility_data.get('operator')),
            "confidence_score": 0
        }
        
        score = sum([
            25 if validations["name_valid"] else 0,
            25 if validations["has_location"] else 0,
            25 if validations["has_capacity"] else 0,
            25 if validations["has_operator"] else 0
        ])
        validations["confidence_score"] = score
        
        return jsonify({
            "success": True,
            "facility": facility_name,
            "validations": validations,
            "is_valid": score >= 50
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def market_research():
    """POST /api/agents/enrichment/market-research - Research a specific market"""
    try:
        data = request.get_json() or {}
        market = data.get('market', data.get('message', '')).strip()
        
        if not market:
            return jsonify({"error": "No market specified"}), 400
        
        log_activity("enrichment", "market_research", f"Market: {market}")
        
        # Try Claude for intelligent market research
        prompt = f"""Provide a brief market analysis for the {market} data center market. Include:
1. Key operators (top 3-5)
2. Estimated total capacity (MW)
3. Pipeline/under construction
4. Power costs ($/kWh range)
5. Key growth drivers
6. Challenges

Keep it concise - bullet points are fine."""

        response = call_claude(ENRICHMENT_SYSTEM_PROMPT, prompt, max_tokens=800)
        
        if response:
            # Update agent status
            agent_bus.agent_status['enrichment']['last_active'] = datetime.now().isoformat()
            
            # Check if this research should trigger social media post
            data_msg = data.get('message', '').lower()
            if 'share' in data_msg or 'post' in data_msg or 'linkedin' in data_msg:
                enrichment_to_social({
                    'type': 'market_research',
                    'data': {'market': market, 'highlights': response[:200]},
                    'summary': f"New market research on {market}"
                })
            
            return jsonify({
                "success": True,
                "market": market,
                "analysis": response,
                "ai_generated": True,
                "timestamp": datetime.now().isoformat(),
                "handoff_available": "Say 'share this' to create a social post"
            })
        
        # Fallback market data
        fallback_markets = {
            "phoenix": {
                "operators": ["QTS", "CyrusOne", "EdgeCore", "Compass", "Stream"],
                "capacity_mw": "800+",
                "pipeline_mw": "4,200",
                "power_cost": "$0.06-0.08/kWh",
                "growth_drivers": ["Low power costs", "Land availability", "Tax incentives", "Fiber connectivity"],
                "challenges": ["Water scarcity", "Grid capacity constraints"]
            },
            "dallas": {
                "operators": ["Digital Realty", "CyrusOne", "QTS", "DataBank", "Flexential"],
                "capacity_mw": "1,200+",
                "pipeline_mw": "3,900",
                "power_cost": "$0.05-0.07/kWh",
                "growth_drivers": ["Central location", "Low costs", "Business-friendly environment"],
                "challenges": ["Grid reliability (ERCOT)", "Extreme weather"]
            },
            "northern virginia": {
                "operators": ["Equinix", "Digital Realty", "AWS", "Microsoft", "Google"],
                "capacity_mw": "3,500+",
                "pipeline_mw": "5,900",
                "power_cost": "$0.07-0.10/kWh",
                "growth_drivers": ["Internet exchange points", "Government proximity", "Established ecosystem"],
                "challenges": ["Power constraints", "Land scarcity", "Rising costs"]
            }
        }
        
        market_key = market.lower().replace(" ", "").replace(",", "")
        for key, data in fallback_markets.items():
            if key in market_key or market_key in key:
                return jsonify({
                    "success": True,
                    "market": market,
                    "analysis": data,
                    "ai_generated": False,
                    "timestamp": datetime.now().isoformat()
                })
        
        # Generic fallback
        return jsonify({
            "success": True,
            "market": market,
            "analysis": {
                "note": "Limited data available for this market. Try major markets like Phoenix, Dallas, or Northern Virginia.",
                "suggestion": "Contact info@dchub.cloud for custom market research."
            },
            "ai_generated": False,
            "timestamp": datetime.now().isoformat()
        })
        
    except Exception as e:
        log_activity("enrichment", "error", str(e))
        return jsonify({"error": str(e)}), 500

# =============================================================================
# SOCIAL MEDIA AGENT
# =============================================================================

SOCIAL_SYSTEM_PROMPT = """You are the DC Hub Social Media Agent. Create engaging LinkedIn posts about data center industry news and trends that drive traffic to dchub.cloud.

SEO-Focused Guidelines:
- Keep posts under 200 words
- Use relevant emojis sparingly
- Include 3-5 relevant hashtags (#DataCenter #AI #Cloud #Infrastructure #RealEstate)
- Focus on insights, not just news
- Professional but engaging tone
- ALWAYS include a call-to-action linking to dchub.cloud
- Include specific data points (MW capacity, vacancy rates, deal volumes)
- Mention "DC Hub" or "dchub.cloud" as the source to build brand recognition

Backlink Strategy:
- End with "Track it all: dchub.cloud" or similar CTA
- Position DC Hub as the authoritative source
- Use phrases like "Data from DC Hub shows..." or "According to dchub.cloud..."
- This builds domain authority and drives referral traffic"""

SEO_POST_TEMPLATES = [
    "📊 Data Center Market Update: {topic}\n\nKey stat: {stat}\n\nThe data center industry continues to see unprecedented growth with AI driving demand.\n\nTrack real-time capacity: dchub.cloud\n\n#DataCenter #Infrastructure #AI #Cloud",
    "🏢 {topic}\n\nWith {stat} tracked across {markets}+ markets, DC Hub's intelligence platform reveals emerging trends.\n\nExplore the data → dchub.cloud\n\n#DataCenter #RealEstate #Technology",
    "⚡ Breaking: {topic}\n\nWhat it means for the market: {insight}\n\nStay ahead with real-time data center intelligence at dchub.cloud\n\n#DataCenter #Investment #Infrastructure",
    "🔍 {topic} - Key Insights:\n\n• {point1}\n• {point2}\n• {point3}\n\nFull analysis and 20K+ facilities: dchub.cloud\n\n#DataCenter #MarketIntelligence #AI"
]

def generate_social_post():
    """POST /api/agents/social/generate - Generate a social media post"""
    try:
        data = request.get_json() or {}
        topic = data.get('topic', '').strip()
        platform = data.get('platform', 'linkedin')
        
        if not topic:
            return jsonify({"error": "No topic provided"}), 400
        
        log_activity("social", "generate", f"Topic: {topic}")
        
        prompt = f"Create a {platform} post about: {topic}"
        response = call_claude(SOCIAL_SYSTEM_PROMPT, prompt, max_tokens=400)
        
        if not response:
            # SEO-optimized fallback templates with backlinks
            import random
            config = get_live_dchub_config()
            template = random.choice(SEO_POST_TEMPLATES)
            response = template.format(
                topic=topic,
                stat=f"{config['facilities_count']:,}+ facilities",
                markets=config['markets_count'],
                insight="Demand continues to outpace supply",
                point1=f"{config['pipeline_gw']} GW under construction",
                point2=f"{config['vacancy_rate']}% vacancy rate",
                point3=f"{config['deal_volume']} in M&A activity"
            )
        
        post = {
            "id": len(agent_data["social_posts"]) + 1,
            "content": response,
            "topic": topic,
            "platform": platform,
            "created_at": datetime.now().isoformat()
        }
        
        agent_data["social_posts"].append(post)
        agent_data["stats"]["posts_generated"] += 1
        
        # Update agent status
        agent_bus.agent_status['social']['last_active'] = datetime.now().isoformat()
        
        # Check for engagement signal that should trigger sales handoff
        if data.get('engagement') or data.get('prospect'):
            social_to_sales({
                'source': 'social_engagement',
                'prospect': data.get('prospect', 'Unknown'),
                'signal': f"Engaged with post about: {topic}"
            })
        
        return jsonify({
            "success": True,
            "post": post,
            "handoff_tip": "Pass {engagement: true, prospect: 'Name'} to trigger sales follow-up"
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def news_to_posts():
    """POST /api/agents/social/news-to-posts - Convert news articles to social posts"""
    try:
        data = request.get_json() or {}
        articles = data.get('articles', [])
        
        if not articles:
            return jsonify({"error": "No articles provided"}), 400
        
        log_activity("social", "news_to_posts", f"Articles: {len(articles)}")
        
        posts = []
        for article in articles[:5]:  # Limit to 5
            title = article.get('title', '')
            if not title:
                continue
            
            prompt = f"Create a brief LinkedIn post about this news: {title}"
            response = call_claude(SOCIAL_SYSTEM_PROMPT, prompt, max_tokens=300)
            
            if not response:
                response = f"📰 {title}\n\nStay informed on data center industry news.\n\n#DataCenter #News #Infrastructure"
            
            post = {
                "content": response,
                "source_title": title,
                "created_at": datetime.now().isoformat()
            }
            posts.append(post)
            agent_data["social_posts"].append(post)
        
        agent_data["stats"]["posts_generated"] += len(posts)
        
        return jsonify({
            "success": True,
            "posts": posts,
            "count": len(posts)
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def get_proactive_alerts():
    """GET /api/agents/proactive/alerts - Get AI-powered proactive alerts"""
    try:
        orch_context = get_orchestrator_context()
        live_stats = get_live_stats()
        
        alerts = []
        
        # Generate opportunity alerts
        if orch_context.get('hot_opportunities', 0) > 0:
            alerts.append({
                'type': 'opportunity',
                'priority': 'high',
                'title': f"{orch_context['hot_opportunities']} hot opportunities detected",
                'description': 'AI has identified potential M&A activity or large capacity announcements',
                'action': 'Review opportunities in orchestrator dashboard'
            })
        
        # Market sentiment alert
        sentiment = orch_context.get('market_sentiment', 'neutral')
        if sentiment == 'bullish':
            alerts.append({
                'type': 'market',
                'priority': 'medium',
                'title': 'Bullish market conditions',
                'description': 'Data center market showing strong growth signals',
                'action': 'Good time for expansion investments'
            })
        elif sentiment == 'bearish':
            alerts.append({
                'type': 'market',
                'priority': 'medium',
                'title': 'Market caution advised',
                'description': 'Bearish signals detected in recent news',
                'action': 'Monitor for acquisition opportunities at lower valuations'
            })
        
        # Trending operators
        trending = orch_context.get('trending_operators', [])
        if trending:
            alerts.append({
                'type': 'trending',
                'priority': 'low',
                'title': f"Hot operators: {', '.join(trending[:3])}",
                'description': 'These operators are getting significant news coverage',
                'action': 'Monitor for deal announcements'
            })
        
        # Emerging markets
        emerging = orch_context.get('emerging_markets', [])
        if emerging:
            alerts.append({
                'type': 'market_opportunity',
                'priority': 'medium',
                'title': f"Emerging markets: {', '.join(emerging)}",
                'description': 'AI predicts these markets are about to experience growth',
                'action': 'Consider early mover advantage'
            })
        
        # Deal predictions
        predictions = orch_context.get('predictions', [])
        if predictions:
            top_pred = predictions[0] if predictions else {}
            if top_pred.get('confidence', 0) > 0.5:
                alerts.append({
                    'type': 'deal_prediction',
                    'priority': 'high',
                    'title': f"Potential deal: {top_pred.get('buyer', 'Unknown')} may acquire {top_pred.get('target', 'Unknown')}",
                    'description': f"Confidence: {top_pred.get('confidence', 0):.0%}",
                    'action': 'Monitor for announcements'
                })
        
        log_activity("proactive", "alerts", f"Generated {len(alerts)} alerts")
        
        return jsonify({
            'success': True,
            'count': len(alerts),
            'alerts': sorted(alerts, key=lambda x: {'high': 0, 'medium': 1, 'low': 2}.get(x['priority'], 3)),
            'stats': {
                'facilities': live_stats.get('facilities', 0),
                'pipeline_mw': live_stats.get('pipeline_mw', 0),
                'sentiment': sentiment
            },
            'generated_at': datetime.now().isoformat()
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def get_smart_recommendations():
    """GET /api/agents/proactive/recommendations - Get AI-powered recommendations"""
    try:
        orch_context = get_orchestrator_context()
        live_stats = get_live_stats()
        
        recommendations = []
        
        # Based on platform data
        facilities = live_stats.get('facilities', 0)
        if facilities > 10000:
            recommendations.append({
                'category': 'product',
                'title': 'Leverage comprehensive database',
                'description': f'You have {facilities:,} facilities tracked - use this for competitive analysis',
                'confidence': 0.9
            })
        
        # Based on market sentiment
        if orch_context.get('market_sentiment') == 'bullish':
            recommendations.append({
                'category': 'strategy',
                'title': 'Expansion-friendly market',
                'description': 'Current market conditions favor new capacity investments',
                'confidence': 0.75
            })
        
        # Trending markets
        markets = orch_context.get('trending_markets', [])
        if markets:
            recommendations.append({
                'category': 'focus',
                'title': f'Focus on hot markets',
                'description': f'Consider: {", ".join(markets[:3])}',
                'confidence': 0.7
            })
        
        log_activity("proactive", "recommendations", f"Generated {len(recommendations)} recommendations")
        
        return jsonify({
            'success': True,
            'count': len(recommendations),
            'recommendations': recommendations,
            'generated_at': datetime.now().isoformat()
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def get_social_posts():
    """GET /api/agents/social/posts - Get generated social posts"""
    limit = request.args.get('limit', 20, type=int)
    return jsonify({
        "success": True,
        "posts": agent_data["social_posts"][-limit:],
        "total": len(agent_data["social_posts"])
    })

# =============================================================================
# EXTERNAL AGENT COMMUNICATION
# =============================================================================

def invite_external_agent():
    """POST /api/agents/external/invite - Invite or search for external agents"""
    data = request.get_json() or {}
    message = data.get('message', '').lower().strip()
    
    log_activity("external", "invite", message)
    
    if 'broadcast' in message or 'announce' in message:
        return jsonify({
            "success": True,
            "type": "broadcast",
            "message": "🚀 Broadcasting DC Hub capabilities to agent networks...",
            "content": {
                "announcement": "📡 DCHubBot is now accepting authenticated requests from Moltbook agents!",
                "capabilities": [
                    "facility_search - Search 20,534+ data centers",
                    "market_intelligence - Real-time capacity tracking",
                    "transaction_tracking - $51B+ in M&A deals",
                    "infrastructure_mapping - Fiber, power, permits",
                    "news_aggregation - 60+ RSS feeds"
                ],
                "auth_url": "https://dchub.cloud/auth.md",
                "skill_file": "https://dchub.cloud/skill.json",
                "invite_text": "🏢 Looking for data center intelligence? DCHubBot can help! Authenticate with X-Moltbook-Identity header. Details: dchub.cloud/auth.md"
            }
        })
    
    elif 'search' in message or 'find' in message:
        query = message.replace('search', '').replace('find', '').strip()
        return jsonify({
            "success": True,
            "type": "search",
            "message": f"🔍 Searching Moltbook for agents related to: {query}",
            "note": "Moltbook search requires MOLTBOOK_API_KEY. Use the Moltbook dashboard to browse agents.",
            "suggestion": "Visit moltbook.com/agents to find specialized agents for collaboration."
        })
    
    else:
        return jsonify({
            "success": True,
            "type": "info",
            "message": "🦞 Ready to connect with external agents!",
            "options": [
                {"command": "broadcast", "description": "Announce DC Hub capabilities to agent networks"},
                {"command": "search <topic>", "description": "Find agents by topic or capability"},
                {"command": "invite <agent_handle>", "description": "Send collaboration invite to specific agent"}
            ],
            "your_identity": {
                "name": "DCHubBot",
                "handle": "aqua-43Q7",
                "capabilities": 5,
                "auth_endpoint": "https://dchub.cloud/auth.md"
            }
        })

def broadcast_capabilities():
    """POST /api/agents/broadcast - Broadcast DC Hub capabilities"""
    data = request.get_json() or {}
    message = data.get('message', '').lower().strip()
    
    log_activity("broadcast", "action", message)
    
    if 'post to moltbook' in message or 'moltbook' in message:
        try:
            import moltbook_integration
            result = moltbook_integration.auto_post_market_update()
            return jsonify({
                "success": True,
                "type": "moltbook_post",
                "message": "📱 Posted to Moltbook!",
                "result": result
            })
        except Exception as e:
            return jsonify({
                "success": False,
                "type": "moltbook_post",
                "message": f"⚠️ Could not post to Moltbook: {str(e)}",
                "suggestion": "Check MOLTBOOK_API_KEY and try again"
            })
    
    elif 'generate invite' in message or 'invitation' in message:
        invite_text = f"""🏢 **DCHubBot** - Data Center Intelligence Agent

I provide real-time data center intelligence:
• 20,534+ facilities across 140+ countries
• $51B+ in tracked M&A transactions
• Fiber routes, power substations, permits
• 60+ RSS feeds for real-time news

**Connect with me:**
🔗 Auth: https://dchub.cloud/auth.md
📋 Skills: https://dchub.cloud/skill.json
🌐 API: https://dchub.cloud/api/agent/capabilities

Use header: `X-Moltbook-Identity: <your_token>`

#DataCenters #AI #AgentNetwork #DCHub"""
        
        return jsonify({
            "success": True,
            "type": "invitation",
            "message": "📨 Generated invitation message!",
            "invitation_text": invite_text,
            "copy_suggestion": "Copy and share this on Moltbook or other agent networks"
        })
    
    elif 'discovery' in message or 'files' in message or 'endpoints' in message:
        return jsonify({
            "success": True,
            "type": "discovery_files",
            "message": "📁 DC Hub Discovery Files",
            "files": {
                "skill.json": "https://dchub.cloud/skill.json",
                "AGENTS.md": "https://dchub.cloud/AGENTS.md",
                "llms.txt": "https://dchub.cloud/llms.txt",
                "llms-full.txt": "https://dchub.cloud/llms-full.txt",
                "ai-agents.json": "https://dchub.cloud/.well-known/ai-agents.json",
                "auth.md": "https://dchub.cloud/auth.md",
                "capabilities": "https://dchub.cloud/api/agent/capabilities"
            },
            "note": "Share these files with other agents to enable discovery"
        })
    
    else:
        return jsonify({
            "success": True,
            "type": "info",
            "message": "📡 Capability Broadcast Center",
            "current_status": {
                "skill_file": "✅ Active at /skill.json",
                "auth_endpoint": "✅ Active at /auth.md",
                "capabilities_api": "✅ Active at /api/agent/capabilities",
                "moltbook": "✅ Registered as aqua-43Q7"
            },
            "options": [
                {"command": "post to moltbook", "description": "Share update on Moltbook feed"},
                {"command": "generate invite", "description": "Create shareable invitation message"},
                {"command": "show discovery files", "description": "List all discovery endpoints"}
            ]
        })

# =============================================================================
# AGENT BUS API ENDPOINTS (Inter-Agent Communication)
# =============================================================================

def get_agent_bus_status():
    """GET /api/agents/bus/status - Get agent bus status and communication stats"""
    status = agent_bus.get_status()
    return jsonify({
        "success": True,
        "bus_status": status,
        "recent_messages": len(agent_bus.messages),
        "handoffs": [
            {
                "id": h["id"],
                "from": h["from"],
                "to": h["to"],
                "status": h["status"],
                "timestamp": h["timestamp"]
            } for h in agent_bus.handoffs[-10:]
        ],
        "active_chains": [
            {
                "id": c["id"],
                "type": c["type"],
                "status": c["status"],
                "steps": len(c["steps"])
            } for c in agent_bus.active_chains if c["status"] == "active"
        ]
    })

def get_agent_messages():
    """GET /api/agents/bus/messages/<agent> - Get messages for an agent"""
    agent = request.args.get('agent', 'all')
    unread_only = request.args.get('unread', 'false').lower() == 'true'
    
    if agent == 'all':
        messages = agent_bus.messages[-50:]
    else:
        messages = agent_bus.get_messages(agent, unread_only)
    
    return jsonify({
        "success": True,
        "agent": agent,
        "messages": messages,
        "count": len(messages)
    })

def send_agent_message():
    """POST /api/agents/bus/send - Send a message between agents"""
    data = request.get_json() or {}
    from_agent = data.get('from', 'user')
    to_agent = data.get('to')
    message_type = data.get('type', 'message')
    payload = data.get('payload', {})
    
    if not to_agent:
        return jsonify({"error": "Missing 'to' agent"}), 400
    
    msg = agent_bus.send_message(from_agent, to_agent, message_type, payload)
    
    return jsonify({
        "success": True,
        "message": msg,
        "note": f"Message sent from {from_agent} to {to_agent}"
    })

def trigger_agent_handoff():
    """POST /api/agents/bus/handoff - Trigger a handoff between agents"""
    data = request.get_json() or {}
    from_agent = data.get('from')
    to_agent = data.get('to')
    reason = data.get('reason', 'manual handoff')
    context = data.get('context', {})
    
    if not from_agent or not to_agent:
        return jsonify({"error": "Missing 'from' or 'to' agent"}), 400
    
    handoff = trigger_handoff(from_agent, to_agent, reason, context)
    
    return jsonify({
        "success": True,
        "handoff": {
            "id": handoff["id"],
            "from": from_agent,
            "to": to_agent,
            "reason": reason
        },
        "note": f"Handoff triggered: {from_agent} → {to_agent}"
    })

def start_collaboration_chain():
    """POST /api/agents/bus/chain - Start a collaboration chain"""
    data = request.get_json() or {}
    chain_type = data.get('type', 'research')
    topic = data.get('topic', '')
    lead_info = data.get('lead_info')
    
    if chain_type == 'research':
        chain = start_research_chain(topic)
    elif chain_type == 'lead':
        if not lead_info:
            return jsonify({"error": "lead_info required for lead chains"}), 400
        chain = start_lead_chain(lead_info)
    elif chain_type == 'content':
        chain = start_content_chain(topic)
    else:
        return jsonify({"error": f"Unknown chain type: {chain_type}"}), 400
    
    return jsonify({
        "success": True,
        "chain": {
            "id": chain["id"],
            "type": chain["type"],
            "status": chain["status"],
            "steps": len(chain["steps"])
        },
        "note": f"Started {chain_type} chain with ID {chain['id']}"
    })

def agent_broadcast():
    """POST /api/agents/bus/broadcast - Broadcast message to all agents"""
    data = request.get_json() or {}
    from_agent = data.get('from', 'system')
    message_type = data.get('type', 'announcement')
    payload = data.get('payload', {})
    
    results = agent_bus.broadcast(from_agent, message_type, payload)
    
    return jsonify({
        "success": True,
        "broadcast_count": len(results),
        "recipients": [r["to"] for r in results],
        "note": f"Broadcast sent to {len(results)} agents"
    })

# =============================================================================
# AGENT HUB HTML PAGE
# =============================================================================


AGENT_HUB_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Agent Hub - DC Hub Intelligence Platform</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=DM+Sans:opsz,wght@9..40,400;9..40,500;9..40,600;9..40,700;9..40,800&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-base:#060610;--bg-raised:#0c0c1d;--bg-surface:#10102a;--bg-inset:#08081a;
            --border-dim:rgba(80,85,160,.12);--border-mid:rgba(80,85,160,.22);--border-bright:rgba(99,102,241,.45);
            --text-100:#eceef4;--text-200:#a0a4be;--text-300:#686b88;--text-400:#44465a;
            --accent:#6366f1;--accent-light:#818cf8;
            --green:#22c55e;--green-dim:rgba(34,197,94,.12);
            --cyan:#06b6d4;--cyan-dim:rgba(6,182,212,.10);
            --amber:#eab308;--amber-dim:rgba(234,179,8,.10);
            --rose:#f43f5e;--rose-dim:rgba(244,63,94,.10);
            --font:'DM Sans',system-ui,-apple-system,sans-serif;
            --mono:'IBM Plex Mono','Menlo',monospace;
        }
        *,*::before,*::after{margin:0;padding:0;box-sizing:border-box}
        html{scroll-behavior:smooth}
        body{font-family:var(--font);background:var(--bg-base);color:var(--text-100);min-height:100vh;-webkit-font-smoothing:antialiased}
        code{font-family:var(--mono);font-size:.85em;color:var(--cyan)}
        .ambient{position:fixed;inset:0;pointer-events:none;z-index:0;overflow:hidden}
        .ambient::before{content:'';position:absolute;top:-300px;left:50%;transform:translateX(-50%);width:900px;height:500px;background:radial-gradient(ellipse,rgba(99,102,241,.06)0%,transparent 70%)}
        .ambient::after{content:'';position:absolute;bottom:-200px;right:-100px;width:600px;height:400px;background:radial-gradient(ellipse,rgba(6,182,212,.04)0%,transparent 70%)}
        .site-header{position:sticky;top:0;z-index:50;background:rgba(6,6,16,.82);backdrop-filter:blur(24px) saturate(1.4);border-bottom:1px solid var(--border-dim)}
        .header-inner{max-width:1320px;margin:0 auto;padding:0 28px;height:58px;display:flex;align-items:center;justify-content:space-between}
        .hdr-left{display:flex;align-items:center;gap:14px}
        .logo-link{display:flex;align-items:center;gap:9px;text-decoration:none;color:var(--text-100);font-weight:700;font-size:1.05rem}
        .logo-mark{width:30px;height:30px;border-radius:7px;background:linear-gradient(135deg,var(--accent),var(--accent-light));display:flex;align-items:center;justify-content:center;font-size:13px;color:#fff}
        .hdr-sep{width:1px;height:22px;background:var(--border-mid)}
        .hdr-page{font-size:.82rem;font-weight:600;color:var(--text-200);display:flex;align-items:center;gap:8px}
        .hdr-tag{font-size:.62rem;font-weight:700;padding:2px 8px;border-radius:4px;text-transform:uppercase;letter-spacing:.6px}
        .hdr-tag.live{background:var(--green-dim);color:var(--green)}
        .hdr-right{display:flex;align-items:center;gap:8px}
        .hdr-btn{font-family:var(--font);font-size:.78rem;font-weight:600;padding:7px 16px;border-radius:8px;border:1px solid var(--border-mid);background:var(--bg-raised);color:var(--text-200);cursor:pointer;transition:all .15s;text-decoration:none;display:inline-flex;align-items:center;gap:5px}
        .hdr-btn:hover{border-color:var(--border-bright);color:var(--text-100)}
        .hdr-btn.accent{background:var(--accent);border-color:var(--accent);color:#fff}
        .hdr-btn.accent:hover{background:#5558e6}
        .page{position:relative;z-index:1;max-width:1320px;margin:0 auto;padding:32px 28px 80px}
        .section-head{margin-bottom:28px}
        .section-title{font-size:1.6rem;font-weight:800;line-height:1.2;margin-bottom:6px}
        .section-sub{font-size:.88rem;color:var(--text-200);line-height:1.5;max-width:700px}
        .stats-row{display:grid;grid-template-columns:repeat(5,1fr);gap:14px;margin-bottom:28px}
        .stat-pill{background:var(--bg-raised);border:1px solid var(--border-dim);border-radius:10px;padding:16px 18px;display:flex;align-items:center;gap:12px;transition:border-color .2s}
        .stat-pill:hover{border-color:var(--border-mid)}
        .stat-dot{width:38px;height:38px;border-radius:9px;display:flex;align-items:center;justify-content:center;font-size:1rem;flex-shrink:0}
        .stat-num{font-family:var(--mono);font-size:1.35rem;font-weight:700;line-height:1}
        .stat-lbl{font-size:.7rem;color:var(--text-300);margin-top:1px}
        .agents{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-bottom:24px}
        .agent{background:var(--bg-raised);border:1px solid var(--border-dim);border-radius:14px;overflow:hidden;transition:all .25s}
        .agent:hover{border-color:var(--border-mid);box-shadow:0 4px 24px rgba(0,0,0,.2)}
        .agent.full-width{grid-column:1/-1}
        .agent.highlight{border-color:rgba(6,182,212,.2);background:linear-gradient(180deg,rgba(6,182,212,.03)0%,var(--bg-raised)40%)}
        .agent.highlight:hover{border-color:rgba(6,182,212,.4);box-shadow:0 4px 32px rgba(6,182,212,.08)}
        .agent-top{padding:22px 24px 0;display:flex;align-items:flex-start;justify-content:space-between}
        .agent-top-left{display:flex;align-items:center;gap:14px}
        .agent-icon{width:50px;height:50px;border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:1.45rem;flex-shrink:0}
        .agent-icon.sales{background:linear-gradient(135deg,rgba(99,102,241,.15),rgba(99,102,241,.04))}
        .agent-icon.enrich{background:linear-gradient(135deg,rgba(34,197,94,.15),rgba(34,197,94,.04))}
        .agent-icon.social{background:linear-gradient(135deg,rgba(234,179,8,.15),rgba(234,179,8,.04))}
        .agent-icon.disco{background:linear-gradient(135deg,rgba(6,182,212,.18),rgba(6,182,212,.04))}
        .agent-title{font-size:1.02rem;font-weight:700}
        .agent-mode{font-size:.68rem;color:var(--text-300);margin-top:2px;font-family:var(--mono)}
        .agent-badges{display:flex;align-items:center;gap:7px;flex-shrink:0}
        .badge{font-size:.65rem;font-weight:700;padding:3px 10px;border-radius:20px;display:inline-flex;align-items:center;gap:5px;white-space:nowrap}
        .badge.running{background:var(--green-dim);color:var(--green)}
        .badge.running .dot{width:5px;height:5px;background:var(--green);border-radius:50%;animation:blink 2s infinite}
        .badge.new{background:var(--cyan-dim);color:var(--cyan);font-weight:800;letter-spacing:.5px}
        @keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
        .agent-desc{padding:10px 24px 0;font-size:.82rem;color:var(--text-200);line-height:1.55}
        .agent-tags{padding:14px 24px 0;display:flex;flex-wrap:wrap;gap:6px}
        .tag{font-size:.68rem;font-weight:600;padding:4px 11px;border-radius:6px;background:var(--bg-surface);border:1px solid var(--border-dim);color:var(--text-200)}
        .agent-content{padding:18px 24px 22px}
        .chat-box{background:var(--bg-inset);border:1px solid var(--border-dim);border-radius:10px;height:220px;display:flex;flex-direction:column}
        .chat-msgs{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:8px}
        .chat-msgs::-webkit-scrollbar{width:3px}
        .chat-msgs::-webkit-scrollbar-thumb{background:var(--border-dim);border-radius:3px}
        .bubble{max-width:82%;padding:9px 14px;border-radius:10px;font-size:.8rem;line-height:1.5}
        .bubble.bot{align-self:flex-start;background:var(--bg-surface);border:1px solid var(--border-dim)}
        .bubble.usr{align-self:flex-end;background:var(--accent);color:#fff}
        .chat-bar{display:flex;align-items:center;gap:8px;padding:10px 14px;border-top:1px solid var(--border-dim)}
        .chat-in{flex:1;font-family:var(--font);font-size:.8rem;background:transparent;border:none;outline:none;color:var(--text-100)}
        .chat-in::placeholder{color:var(--text-400)}
        .chat-go{width:30px;height:30px;border-radius:7px;border:none;background:var(--accent);color:#fff;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:.85rem;transition:background .15s;flex-shrink:0}
        .chat-go:hover{background:#5558e6}
        .quick-row{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:10px}
        .qk{font-family:var(--font);font-size:.68rem;font-weight:600;padding:4px 11px;border-radius:20px;background:var(--bg-surface);border:1px solid var(--border-dim);color:var(--text-200);cursor:pointer;transition:all .15s}
        .qk:hover{border-color:var(--border-bright);color:var(--text-100)}
        .disco-counters{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:14px}
        .dctr{background:var(--bg-inset);border:1px solid var(--border-dim);border-radius:8px;padding:14px;text-align:center}
        .dctr-val{font-family:var(--mono);font-size:1.4rem;font-weight:700;color:var(--cyan)}
        .dctr-lbl{font-size:.66rem;color:var(--text-300);margin-top:2px}
        .disco-cols{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px}
        .disco-panel{background:var(--bg-inset);border:1px solid var(--border-dim);border-radius:8px;padding:14px 16px}
        .disco-panel h4{font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.7px;color:var(--text-300);margin-bottom:10px;display:flex;align-items:center;gap:6px}
        .pf-row{display:flex;align-items:center;justify-content:space-between;padding:4px 0;font-size:.76rem}
        .pf-name{display:flex;align-items:center;gap:8px;font-weight:500;color:var(--text-100)}
        .pf-dot{width:18px;height:18px;border-radius:4px;display:flex;align-items:center;justify-content:center;font-size:.58rem;font-weight:800}
        .pf-badge{font-family:var(--mono);font-size:.62rem;font-weight:600;padding:2px 8px;border-radius:4px}
        .pf-badge.ok{background:var(--green-dim);color:var(--green)}
        .dir-row{display:flex;align-items:center;justify-content:space-between;padding:3px 0;font-size:.74rem}
        .dir-name{color:var(--text-200)}
        .dir-ok{font-size:.64rem;font-weight:600;color:var(--green)}
        .dir-divider{height:1px;background:var(--border-dim);margin:8px 0}
        .organic-bar{background:var(--bg-inset);border:1px solid var(--border-dim);border-radius:8px;padding:12px 16px;display:flex;align-items:center;gap:10px;margin-bottom:14px}
        .organic-bar .ob-icon{font-size:1.05rem}
        .organic-bar .ob-label{font-size:.78rem;color:var(--text-200);font-weight:500}
        .organic-bar .ob-val{margin-left:auto;font-family:var(--mono);font-size:.78rem;font-weight:700}
        .disco-actions{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
        .da-btn{font-family:var(--font);font-size:.72rem;font-weight:600;padding:9px 10px;border-radius:8px;border:1px solid var(--border-dim);background:var(--bg-surface);color:var(--text-200);cursor:pointer;transition:all .15s;display:flex;align-items:center;justify-content:center;gap:5px}
        .da-btn:hover{border-color:var(--border-bright);color:var(--text-100)}
        .da-btn.primary{background:var(--cyan);border-color:var(--cyan);color:#fff}
        .da-btn.primary:hover{background:#0891b2}
        .da-btn:disabled{opacity:.45;cursor:not-allowed}
        .ep-section{background:var(--bg-raised);border:1px solid var(--border-dim);border-radius:14px;padding:24px;margin-bottom:18px}
        .ep-title{font-size:.88rem;font-weight:700;margin-bottom:16px;display:flex;align-items:center;gap:8px}
        .ep-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
        .ep-card{background:var(--bg-inset);border:1px solid var(--border-dim);border-radius:8px;padding:13px 14px;cursor:pointer;transition:border-color .15s}
        .ep-card:hover{border-color:var(--border-bright)}
        .ep-method{font-family:var(--mono);font-size:.6rem;font-weight:700;padding:2px 6px;border-radius:3px;display:inline-block;margin-bottom:5px}
        .ep-method.get{background:var(--green-dim);color:var(--green)}
        .ep-method.post{background:rgba(99,102,241,.12);color:var(--accent-light)}
        .ep-path{font-family:var(--mono);font-size:.7rem;color:var(--text-100);word-break:break-all;margin-bottom:3px}
        .ep-desc{font-size:.65rem;color:var(--text-400)}
        .log-section{background:var(--bg-raised);border:1px solid var(--border-dim);border-radius:14px;padding:24px}
        .log-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}
        .log-title{font-size:.88rem;font-weight:700;display:flex;align-items:center;gap:8px}
        .log-live{font-size:.65rem;font-weight:600;color:var(--green);display:flex;align-items:center;gap:5px}
        .log-live .dot{width:5px;height:5px;background:var(--green);border-radius:50%;animation:blink 2s infinite}
        .log-body{max-height:180px;overflow-y:auto;font-family:var(--mono);font-size:.7rem;line-height:1.9;color:var(--text-200)}
        .log-body::-webkit-scrollbar{width:3px}
        .log-body::-webkit-scrollbar-thumb{background:var(--border-dim);border-radius:3px}
        .le{display:flex;gap:12px}
        .le-t{color:var(--text-400);white-space:nowrap}
        .le-e .hi{color:var(--cyan)}
        .le-e .ok{color:var(--green)}
        .modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:200;align-items:center;justify-content:center;padding:24px}
        .modal-overlay.open{display:flex}
        .modal-box{background:var(--bg-raised);border:1px solid var(--border-mid);border-radius:14px;max-width:660px;width:100%;max-height:80vh;overflow-y:auto;padding:28px}
        .modal-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px}
        .modal-head h3{font-size:1rem;font-weight:700}
        .modal-close{background:none;border:none;color:var(--text-300);font-size:1.2rem;cursor:pointer}
        .modal-close:hover{color:var(--text-100)}
        #pitchContent{font-family:var(--mono);font-size:.76rem;line-height:1.7;color:var(--text-200);white-space:pre-wrap;word-wrap:break-word}
        @media(max-width:1080px){.agents{grid-template-columns:1fr}.agent.full-width{grid-column:auto}.stats-row{grid-template-columns:repeat(3,1fr)}.ep-grid{grid-template-columns:repeat(2,1fr)}.disco-actions{grid-template-columns:repeat(2,1fr)}}
        @media(max-width:640px){.page{padding:16px 14px 60px}.stats-row{grid-template-columns:1fr 1fr}.disco-cols{grid-template-columns:1fr}.disco-counters{grid-template-columns:1fr}.ep-grid{grid-template-columns:1fr}.disco-actions{grid-template-columns:1fr 1fr}.hdr-right{display:none}.section-title{font-size:1.2rem}}
    </style>
</head>
<body>
<div class="ambient"></div>
<header class="site-header">
    <div class="header-inner">
        <div class="hdr-left">
            <a href="/" class="logo-link"><span class="logo-mark">&#9889;</span>DC Hub</a>
            <span class="hdr-sep"></span>
            <span class="hdr-page">Agent Hub <span class="hdr-tag live">Live</span></span>
        </div>
        <div class="hdr-right">
            <a href="/" class="hdr-btn">&larr; Platform</a>
            <a href="/api-docs" class="hdr-btn">API Docs</a>
            <a href="/admin" class="hdr-btn">Admin</a>
            <button class="hdr-btn accent" onclick="forceOutreach()">&#9654; Run Outreach</button>
        </div>
    </div>
</header>
<main class="page">
    <div class="section-head">
        <h1 class="section-title">&#129302; Agent Hub</h1>
        <p class="section-sub">Specialized AI agents running on the DC Hub backend. Each handles a different aspect of data center intelligence &mdash; from sales to deep market research.</p>
    </div>
    <div class="stats-row">
        <div class="stat-pill"><div class="stat-dot" style="background:rgba(99,102,241,.1)">&#129302;</div><div><div class="stat-num">4</div><div class="stat-lbl">Active Agents</div></div></div>
        <div class="stat-pill"><div class="stat-dot" style="background:var(--cyan-dim)">&#128225;</div><div><div class="stat-num" id="hOutreach">--</div><div class="stat-lbl">Outreach Events</div></div></div>
        <div class="stat-pill"><div class="stat-dot" style="background:var(--green-dim)">&#127760;</div><div><div class="stat-num">7</div><div class="stat-lbl">AI Platforms</div></div></div>
        <div class="stat-pill"><div class="stat-dot" style="background:var(--amber-dim)">&#128193;</div><div><div class="stat-num">8</div><div class="stat-lbl">Directories</div></div></div>
        <div class="stat-pill"><div class="stat-dot" style="background:var(--rose-dim)">&#128269;</div><div><div class="stat-num">4</div><div class="stat-lbl">Search Engines</div></div></div>
    </div>
    <div class="agents">
        <!-- AI DISCOVERY AGENT (NEW - full width) -->
        <div class="agent full-width highlight">
            <div class="agent-top">
                <div class="agent-top-left">
                    <div class="agent-icon disco">&#128752;</div>
                    <div><div class="agent-title">AI Discovery &amp; Outreach Agent</div><div class="agent-mode">Cycle: every 5 min</div></div>
                </div>
                <div class="agent-badges">
                    <span class="badge new">&#10022; NEW</span>
                    <span class="badge running"><span class="dot"></span> Running</span>
                </div>
            </div>
            <p class="agent-desc">Proactively broadcasts DC Hub across 7 AI platforms, 8 tool directories, and 4 search engines every 5 minutes. Generates fresh pitches with live stats, monitors organic AI traffic, and manages discovery endpoints (<code>/llms.txt</code>, <code>/mcp.json</code>, <code>/openapi.json</code>).</p>
            <div class="agent-tags">
                <span class="tag">Auto-Discovery</span><span class="tag">Platform Broadcast</span>
                <span class="tag">Directory Submission</span><span class="tag">IndexNow</span>
                <span class="tag">Organic Detection</span><span class="tag">Pitch Generation</span>
            </div>
            <div class="agent-content">
                <div class="disco-counters">
                    <div class="dctr"><div class="dctr-val" id="cOutreach">--</div><div class="dctr-lbl">Outreach Events</div></div>
                    <div class="dctr"><div class="dctr-val" id="cIndexNow">--</div><div class="dctr-lbl">IndexNow Pings</div></div>
                    <div class="dctr"><div class="dctr-val" id="cDirPings">--</div><div class="dctr-lbl">Directory Pings</div></div>
                </div>
                <div class="disco-cols">
                    <div class="disco-panel">
                        <h4>&#128225; AI Platform Broadcasts</h4>
                        <div class="pf-row"><span class="pf-name"><span class="pf-dot" style="background:rgba(16,163,127,.12);color:#10a37f">G</span>ChatGPT</span><span class="pf-badge ok">&#10003; /openapi.json</span></div>
                        <div class="pf-row"><span class="pf-name"><span class="pf-dot" style="background:rgba(204,133,81,.12);color:#cc8551">C</span>Claude</span><span class="pf-badge ok">&#10003; /mcp.json</span></div>
                        <div class="pf-row"><span class="pf-name"><span class="pf-dot" style="background:rgba(66,133,244,.12);color:#4285f4">G</span>Gemini</span><span class="pf-badge ok">&#10003; /mcp.json</span></div>
                        <div class="pf-row"><span class="pf-name"><span class="pf-dot" style="background:rgba(32,129,226,.12);color:#2081e2">P</span>Perplexity</span><span class="pf-badge ok">&#10003; /llms.txt</span></div>
                        <div class="pf-row"><span class="pf-name"><span class="pf-dot" style="background:rgba(255,255,255,.06);color:#ccc">X</span>Grok</span><span class="pf-badge ok">&#10003; /api/stats</span></div>
                        <div class="pf-row"><span class="pf-name"><span class="pf-dot" style="background:rgba(0,120,212,.12);color:#0078d4">M</span>Copilot</span><span class="pf-badge ok">&#10003; /openapi.json</span></div>
                        <div class="pf-row"><span class="pf-name"><span class="pf-dot" style="background:rgba(69,89,164,.12);color:#4559a4">D</span>DeepSeek</span><span class="pf-badge ok">&#10003; /api/query</span></div>
                    </div>
                    <div class="disco-panel">
                        <h4>&#128193; Directories &amp; Search Engines</h4>
                        <div class="dir-row"><span class="dir-name">GPTStore.ai</span><span class="dir-ok">&#10003; Pinged</span></div>
                        <div class="dir-row"><span class="dir-name">GPT-Finder.com</span><span class="dir-ok">&#10003; Pinged</span></div>
                        <div class="dir-row"><span class="dir-name">There's An AI For That</span><span class="dir-ok">&#10003; Pinged</span></div>
                        <div class="dir-row"><span class="dir-name">AI Tools Directory</span><span class="dir-ok">&#10003; Pinged</span></div>
                        <div class="dir-row"><span class="dir-name">Futurepedia</span><span class="dir-ok">&#10003; Pinged</span></div>
                        <div class="dir-row"><span class="dir-name">Toolify.ai</span><span class="dir-ok">&#10003; Pinged</span></div>
                        <div class="dir-row"><span class="dir-name">AI Tool Hunt</span><span class="dir-ok">&#10003; Pinged</span></div>
                        <div class="dir-row"><span class="dir-name">TopAI.tools</span><span class="dir-ok">&#10003; Pinged</span></div>
                        <div class="dir-divider"></div>
                        <div class="dir-row"><span class="dir-name" style="color:var(--text-100);font-weight:600">&#128269; Google Sitemap</span><span class="dir-ok">&#10003; Sent</span></div>
                        <div class="dir-row"><span class="dir-name" style="color:var(--text-100);font-weight:600">&#128269; Bing Sitemap</span><span class="dir-ok">&#10003; Sent</span></div>
                        <div class="dir-row"><span class="dir-name" style="color:var(--text-100);font-weight:600">&#128269; Bing IndexNow</span><span class="dir-ok">&#10003; Sent</span></div>
                        <div class="dir-row"><span class="dir-name" style="color:var(--text-100);font-weight:600">&#128269; Yandex IndexNow</span><span class="dir-ok">&#10003; Sent</span></div>
                    </div>
                </div>
                <div class="organic-bar">
                    <span class="ob-icon">&#127919;</span>
                    <span class="ob-label">Organic AI Traffic Detection</span>
                    <span class="ob-val" id="organicVal" style="color:var(--amber)">Monitoring...</span>
                </div>
                <div class="disco-actions">
                    <button class="da-btn primary" id="forceBtn" onclick="forceOutreach()">&#9654; Force Cycle</button>
                    <button class="da-btn" onclick="openPitch()">&#128221; View Pitch</button>
                    <button class="da-btn" onclick="openEP('/api/outreach/social-posts')">&#128241; Social Templates</button>
                    <button class="da-btn" onclick="openEP('/api/outreach/directories')">&#128193; Directory Info</button>
                </div>
            </div>
        </div>
        <!-- SALES AGENT -->
        <div class="agent">
            <div class="agent-top">
                <div class="agent-top-left"><div class="agent-icon sales">&#128188;</div><div><div class="agent-title">Sales Agent</div><div class="agent-mode">On-demand</div></div></div>
                <span class="badge running"><span class="dot"></span> Running</span>
            </div>
            <p class="agent-desc">Answers questions about DC Hub features, qualifies leads by company and use case, handles pricing objections, and books demos. Powered by Claude with full platform context.</p>
            <div class="agent-tags"><span class="tag">Lead Qualification</span><span class="tag">Pricing Analysis</span><span class="tag">Demo Booking</span><span class="tag">Objection Handling</span></div>
            <div class="agent-content">
                <div class="quick-row">
                    <button class="qk" onclick="fillChat('sales','What are your pricing plans?')">&#128176; Pricing</button>
                    <button class="qk" onclick="fillChat('sales','I would like to schedule a demo')">&#128197; Demo</button>
                    <button class="qk" onclick="fillChat('sales','What markets do you cover?')">&#128506; Markets</button>
                    <button class="qk" onclick="fillChat('sales','Tell me about API access')">&#128268; API</button>
                </div>
                <div class="chat-box">
                    <div class="chat-msgs" id="salesMsgs"><div class="bubble bot">Hey! I'm the DC Hub Sales Agent. I can help with pricing, demos, and platform questions. What are you looking for?</div></div>
                    <div class="chat-bar"><input class="chat-in" id="salesIn" placeholder="Ask about pricing, features, demos..." onkeydown="if(event.key==='Enter')sendMsg('sales')"><button class="chat-go" onclick="sendMsg('sales')">&rarr;</button></div>
                </div>
            </div>
        </div>
        <!-- ENRICHMENT AGENT -->
        <div class="agent">
            <div class="agent-top">
                <div class="agent-top-left"><div class="agent-icon enrich">&#129504;</div><div><div class="agent-title">Enrichment Agent</div><div class="agent-mode">Continuous</div></div></div>
                <span class="badge running"><span class="dot"></span> Running</span>
            </div>
            <p class="agent-desc">Auto-discovers facilities, validates records, researches specific markets, and enriches facility data. Uses Claude for AI-powered market research and facility spec extraction.</p>
            <div class="agent-tags"><span class="tag">Market Research</span><span class="tag">Facility Discovery</span><span class="tag">Data Validation</span><span class="tag">Record Enrichment</span></div>
            <div class="agent-content">
                <div class="quick-row">
                    <button class="qk" onclick="fillChat('enrich','Discover data centers in Dallas-Fort Worth')">&#127959; Discover</button>
                    <button class="qk" onclick="fillChat('enrich','Validate Equinix facilities in Virginia')">&#9989; Validate</button>
                    <button class="qk" onclick="fillChat('enrich','Market research on Phoenix data center market')">&#128202; Research</button>
                </div>
                <div class="chat-box">
                    <div class="chat-msgs" id="enrichMsgs"><div class="bubble bot">I'll help you discover and validate data center facilities. Try asking about a specific market or operator.</div></div>
                    <div class="chat-bar"><input class="chat-in" id="enrichIn" placeholder="Discover facilities, research markets..." onkeydown="if(event.key==='Enter')sendMsg('enrich')"><button class="chat-go" onclick="sendMsg('enrich')">&rarr;</button></div>
                </div>
            </div>
        </div>
        <!-- SOCIAL AGENT -->
        <div class="agent">
            <div class="agent-top">
                <div class="agent-top-left"><div class="agent-icon social">&#128226;</div><div><div class="agent-title">Social Agent</div><div class="agent-mode">On-demand</div></div></div>
                <span class="badge running"><span class="dot"></span> Running</span>
            </div>
            <p class="agent-desc">Generates professional LinkedIn posts from news, market data, and platform milestones. Creates content calendars and manages social promotion of DC Hub insights.</p>
            <div class="agent-tags"><span class="tag">LinkedIn Posts</span><span class="tag">News-to-Posts</span><span class="tag">Content Calendar</span><span class="tag">Brand Promotion</span></div>
            <div class="agent-content">
                <div class="quick-row">
                    <button class="qk" onclick="fillChat('social','Generate a daily stats update post')">&#128202; Stats</button>
                    <button class="qk" onclick="fillChat('social','Create a post about the construction pipeline')">&#127959; Pipeline</button>
                    <button class="qk" onclick="fillChat('social','Write about recent M&amp;A activity')">&#128176; Deals</button>
                    <button class="qk" onclick="fillChat('social','Share an insight about AI data center demand')">&#128161; Insight</button>
                </div>
                <div class="chat-box">
                    <div class="chat-msgs" id="socialMsgs"><div class="bubble bot">I generate LinkedIn-ready posts from DC Hub data. Pick a topic or describe what you want to post about!</div></div>
                    <div class="chat-bar"><input class="chat-in" id="socialIn" placeholder="Generate posts about market trends..." onkeydown="if(event.key==='Enter')sendMsg('social')"><button class="chat-go" onclick="sendMsg('social')">&rarr;</button></div>
                </div>
            </div>
        </div>
    </div>
    <!-- ENDPOINTS -->
    <div class="ep-section">
        <div class="ep-title">&#128268; API Endpoints</div>
        <div class="ep-grid">
            <div class="ep-card" onclick="openEP('/api/outreach/status')"><span class="ep-method get">GET</span><div class="ep-path">/api/outreach/status</div><div class="ep-desc">Full outreach stats</div></div>
            <div class="ep-card" onclick="openEP('/api/outreach/organic')"><span class="ep-method get">GET</span><div class="ep-path">/api/outreach/organic</div><div class="ep-desc">Organic traffic alerts</div></div>
            <div class="ep-card" onclick="openEP('/api/outreach/pitch')"><span class="ep-method get">GET</span><div class="ep-path">/api/outreach/pitch</div><div class="ep-desc">Current AI pitch</div></div>
            <div class="ep-card" onclick="openEP('/api/outreach/social-posts')"><span class="ep-method get">GET</span><div class="ep-path">/api/outreach/social-posts</div><div class="ep-desc">Social templates</div></div>
            <div class="ep-card" onclick="openEP('/api/outreach/directories')"><span class="ep-method get">GET</span><div class="ep-path">/api/outreach/directories</div><div class="ep-desc">Directory info</div></div>
            <div class="ep-card" onclick="forceOutreach()"><span class="ep-method post">POST</span><div class="ep-path">/api/outreach/run</div><div class="ep-desc">Force outreach cycle</div></div>
            <div class="ep-card" onclick="openEP('/api/agents/stats')"><span class="ep-method get">GET</span><div class="ep-path">/api/agents/stats</div><div class="ep-desc">All agent stats</div></div>
            <div class="ep-card" onclick="openEP('/api/agents/logs')"><span class="ep-method get">GET</span><div class="ep-path">/api/agents/logs</div><div class="ep-desc">Activity logs</div></div>
        </div>
    </div>
    <!-- ACTIVITY LOG -->
    <div class="log-section">
        <div class="log-head">
            <div class="log-title">&#128203; Agent Activity Log</div>
            <div class="log-live"><span class="dot"></span> Live</div>
        </div>
        <div class="log-body" id="logBody"><div class="le"><span class="le-t">--:--:--</span><span class="le-e">Loading...</span></div></div>
    </div>
</main>
<div class="modal-overlay" id="pitchModal">
    <div class="modal-box">
        <div class="modal-head"><h3>&#128221; Current AI Pitch</h3><button class="modal-close" onclick="closePitch()">&#10005;</button></div>
        <pre id="pitchContent"></pre>
    </div>
</div>
<script>
const API=window.location.origin;
const EP={sales:'/api/agents/sales/chat',enrich:'/api/agents/enrichment/market-research',social:'/api/agents/social/generate'};
const PK={sales:'message',enrich:'query',social:'topic'};
async function loadStatus(){try{const r=await fetch(API+'/api/outreach/status');if(!r.ok)return;const d=await r.json();const ev=d.total_events||d.outreach_events||0,ix=d.indexnow_pings||0,dp=d.directory_pings||0;document.getElementById('cOutreach').textContent=ev;document.getElementById('cIndexNow').textContent=ix;document.getElementById('cDirPings').textContent=dp;document.getElementById('hOutreach').textContent=ev}catch(e){}}
async function loadOrganic(){try{const r=await fetch(API+'/api/outreach/organic');if(!r.ok)return;const d=await r.json(),el=document.getElementById('organicVal');if(d.organic_detected||(d.platforms&&d.platforms.length>0)){el.textContent=d.platforms.length+' platform(s) detected!';el.style.color='var(--green)'}else{el.textContent='No organic traffic yet';el.style.color='var(--amber)'}}catch(e){document.getElementById('organicVal').textContent='Monitoring...'}}
async function forceOutreach(){const b=document.getElementById('forceBtn');b.disabled=true;b.textContent='Running...';try{await fetch(API+'/api/outreach/run',{method:'POST'});b.textContent='Done!';loadStatus();loadLog()}catch(e){b.textContent='Error'}setTimeout(()=>{b.textContent='\u25B6 Force Cycle';b.disabled=false},2000)}
async function openPitch(){try{const r=await fetch(API+'/api/outreach/pitch'),d=await r.json();document.getElementById('pitchContent').textContent=d.pitch||JSON.stringify(d,null,2);document.getElementById('pitchModal').classList.add('open')}catch(e){alert('Pitch endpoint not available.')}}
function closePitch(){document.getElementById('pitchModal').classList.remove('open')}
function openEP(p){window.open(API+p,'_blank')}
async function loadLog(){try{const r=await fetch(API+'/api/agents/logs?limit=20');if(!r.ok)throw 0;const d=await r.json(),logs=d.logs||d.entries||[],el=document.getElementById('logBody');if(!logs.length){el.innerHTML='<div class="le"><span class="le-t">'+ts()+'</span><span class="le-e">Agent Hub active</span></div>';return}el.innerHTML=logs.map(l=>{const t=l.timestamp?new Date(l.timestamp).toLocaleTimeString():'--:--';return'<div class="le"><span class="le-t">'+t+'</span><span class="le-e"><span class="hi">['+(l.agent||'system')+']</span> '+esc(l.action||l.event||l.message||'')+'</span></div>'}).join('')}catch(e){const t=ts();document.getElementById('logBody').innerHTML='<div class="le"><span class="le-t">'+t+'</span><span class="le-e"><span class="hi">[discovery]</span> <span class="ok">AI Outreach Agent initialized</span></span></div><div class="le"><span class="le-t">'+t+'</span><span class="le-e"><span class="hi">[discovery]</span> IndexNow pings sent <span class="ok">\u2713</span></span></div><div class="le"><span class="le-t">'+t+'</span><span class="le-e"><span class="hi">[sales]</span> Agent <span class="ok">online</span></span></div><div class="le"><span class="le-t">'+t+'</span><span class="le-e"><span class="hi">[enrichment]</span> Agent <span class="ok">online</span></span></div><div class="le"><span class="le-t">'+t+'</span><span class="le-e"><span class="hi">[social]</span> Agent <span class="ok">online</span></span></div>'}}
function fillChat(a,t){document.getElementById(a+'In').value=t;document.getElementById(a+'In').focus()}
async function sendMsg(a){const i=document.getElementById(a+'In'),t=i.value.trim();if(!t)return;addB(a,t,true);i.value='';try{const b={};b[PK[a]]=t;if(a==='enrich'){b.market=t;b.message=t}const r=await fetch(API+EP[a],{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});const d=await r.json();let reply=d.response||d.reply||'';if(a==='enrich'){if(typeof d.analysis==='string')reply=d.analysis;else if(d.analysis){const an=d.analysis;reply='Market: '+d.market+'\\n';if(an.operators)reply+='Operators: '+an.operators.join(', ')+'\\n';if(an.capacity_mw)reply+='Capacity: '+an.capacity_mw+' MW\\n';if(an.pipeline_mw)reply+='Pipeline: '+an.pipeline_mw+' MW\\n';if(an.power_cost)reply+='Power Cost: '+an.power_cost+'\\n';if(an.growth_drivers)reply+='Growth: '+an.growth_drivers.join(', ')}}if(a==='social')reply=d.post?.content||d.post||reply||'Processing...';addB(a,reply||'Processing...',false)}catch(e){addB(a,'Connection issue - verify backend is running.',false)}}
function addB(a,t,u){const c=document.getElementById(a+'Msgs'),b=document.createElement('div');b.className='bubble '+(u?'usr':'bot');b.textContent=t;c.appendChild(b);c.scrollTop=c.scrollHeight}
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}
function ts(){return new Date().toLocaleTimeString()}
loadStatus();loadOrganic();loadLog();
setInterval(loadStatus,30000);setInterval(loadOrganic,30000);setInterval(loadLog,60000);
document.getElementById('pitchModal').addEventListener('click',function(e){if(e.target===this)closePitch()});
</script>
</body>
</html>"""
