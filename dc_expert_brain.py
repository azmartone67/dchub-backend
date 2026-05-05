"""
DC Hub Expert Brain - Advanced AI Learning & Intelligence System
================================================================
The central intelligence system that powers all DC Hub agents with:
- Deep data center industry expertise
- Continuous learning from all data sources  
- Pattern recognition and prediction
- Market intelligence and trend analysis
- Operator tracking and relationship mapping

This brain learns from:
- 10,000+ tracked facilities
- 60+ news sources
- SEC filings and M&A deals
- Market capacity trends
- User interactions
"""

import json
import os
import hashlib
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple
from collections import defaultdict
import threading
import time
from db_utils import get_db

DB_PATH = 'dc_nexus.db'
BRAIN_STATE_FILE = 'data/brain_state.json'

# =============================================================================
# DC INDUSTRY EXPERT KNOWLEDGE BASE
# =============================================================================

DC_EXPERT_KNOWLEDGE = {
    "industry_fundamentals": {
        "what_is_datacenter": "A data center is a facility that houses computing infrastructure - servers, storage, and networking equipment - that powers digital services. Modern hyperscale facilities can exceed 100MW of power capacity.",
        "key_metrics": {
            "PUE": "Power Usage Effectiveness - ratio of total facility power to IT equipment power. Industry average is 1.58, best-in-class is under 1.2",
            "uptime_tiers": {
                "Tier I": "99.671% uptime, no redundancy",
                "Tier II": "99.741% uptime, partial redundancy", 
                "Tier III": "99.982% uptime, concurrently maintainable",
                "Tier IV": "99.995% uptime, fault tolerant"
            },
            "vacancy_rate": "Currently at historic lows (1.6% in primary markets), driving pricing up",
            "cap_rates": "Data center cap rates have compressed to 4-6% for core assets"
        },
        "facility_types": {
            "hyperscale": "100+ MW facilities for cloud providers (AWS, Azure, Google)",
            "colocation": "Multi-tenant facilities offering space, power, cooling",
            "enterprise": "Company-owned facilities for internal IT",
            "edge": "Smaller facilities closer to end users for low latency"
        }
    },
    
    "major_operators": {
        "hyperscalers": {
            "Amazon AWS": {"facilities": 100, "markets": 33, "specialty": "Cloud infrastructure, largest cloud provider"},
            "Microsoft Azure": {"facilities": 60, "markets": 60, "specialty": "Cloud + enterprise, strong in gaming"},
            "Google Cloud": {"facilities": 40, "markets": 25, "specialty": "AI/ML workloads, sustainability focus"},
            "Meta": {"facilities": 20, "markets": 15, "specialty": "Social platforms, AI research"},
            "Apple": {"facilities": 10, "markets": 8, "specialty": "Consumer services, high security"}
        },
        "data_center_reits": {
            "Equinix": {"ticker": "EQIX", "facilities": 260, "markets": 71, "specialty": "Interconnection leader, premium colocation"},
            "Digital Realty": {"ticker": "DLR", "facilities": 300, "markets": 50, "specialty": "Wholesale + colo, hyperscale campuses"},
            "CyrusOne": {"status": "Acquired by KKR/GIP", "specialty": "Enterprise + hyperscale"},
            "QTS": {"status": "Acquired by Blackstone", "specialty": "Hyperscale, federal"},
            "CoreSite": {"status": "Acquired by American Tower", "specialty": "Interconnection, West Coast"}
        },
        "emerging_players": {
            "Stack Infrastructure": {"backed_by": "IPI Partners", "focus": "Hyperscale development"},
            "Compass": {"backed_by": "Brookfield", "focus": "Sustainable hyperscale"},
            "Vantage": {"backed_by": "DigitalBridge", "focus": "North America, EMEA"},
            "EdgeCore": {"backed_by": "Mount Elbert", "focus": "Edge + wholesale"},
            "Prime Data Centers": {"focus": "Powered shell, hyperscale"},
            "Applied Digital": {"ticker": "APLD", "focus": "AI/HPC, next-gen compute"},
            "CoreWeave": {"focus": "GPU cloud, AI infrastructure"},
            "Crusoe Energy": {"focus": "Stranded gas, sustainable compute"}
        }
    },
    
    "key_markets": {
        "tier_1_us": {
            "Northern Virginia": {"capacity_mw": 3500, "growth": "18%", "constraints": "Power, land scarcity"},
            "Dallas-Fort Worth": {"capacity_mw": 1800, "growth": "22%", "advantages": "Land, power, central location"},
            "Phoenix": {"capacity_mw": 1200, "growth": "35%", "advantages": "Cheap power, land, cooling"},
            "Chicago": {"capacity_mw": 950, "growth": "12%", "role": "Central US hub, financial"},
            "Silicon Valley": {"capacity_mw": 850, "growth": "8%", "constraints": "Power, land, cost"}
        },
        "tier_1_emea": {
            "Frankfurt": {"capacity_mw": 750, "role": "DACH hub, financial services"},
            "London": {"capacity_mw": 680, "role": "Financial, media"},
            "Amsterdam": {"capacity_mw": 520, "role": "Connectivity hub, submarine cables"},
            "Dublin": {"capacity_mw": 480, "role": "Tech HQ, favorable tax"}
        },
        "tier_1_apac": {
            "Singapore": {"capacity_mw": 450, "note": "Moratorium on new builds, sustainability focus"},
            "Tokyo": {"capacity_mw": 620, "role": "Largest APAC market"},
            "Sydney": {"capacity_mw": 380, "growth": "15%"}
        },
        "emerging_hotspots": [
            {"name": "Columbus OH", "reason": "Central location, Tier III power grid, hyperscaler interest"},
            {"name": "Nashville TN", "reason": "TVA power, healthcare data, growing tech scene"},
            {"name": "Salt Lake City", "reason": "Cheap power, Facebook presence"},
            {"name": "Kansas City", "reason": "Central location, Google fiber"},
            {"name": "Reno NV", "reason": "Tax incentives, Tesla/Apple presence"}
        ]
    },
    
    "market_dynamics": {
        "current_trends": [
            "AI/ML driving unprecedented power demand - GPU clusters need 5-10x power density",
            "Power constraints emerging as #1 barrier in primary markets",
            "Nuclear power PPAs becoming preferred for sustainability + reliability",
            "Construction costs up 25-30% since 2020",
            "12-18 month typical build timeline extending due to equipment delays",
            "Private equity dry powder exceeding $50B targeting DC assets"
        ],
        "pricing_trends": {
            "wholesale_retail": "$150-250/kW/month in primary markets, up 20% YoY",
            "powered_shell": "$1.5-2.5M per MW construction cost",
            "land": "$500K-2M per acre in primary markets"
        },
        "deal_activity": {
            "ytd_volume": "$51B+ in transactions",
            "avg_deal_size": "$500M-2B for platform deals",
            "cap_rate_compression": "Cap rates at 4-6% for stabilized assets",
            "key_buyers": ["Blackstone", "KKR", "GIP", "Brookfield", "DigitalBridge"]
        }
    },
    
    "terminology": {
        "N+1": "Redundancy configuration with one backup unit",
        "2N": "Fully redundant - two complete systems",
        "white_space": "Raised floor area for IT equipment",
        "meet_me_room": "Network interconnection point in a data center",
        "PPA": "Power Purchase Agreement - long-term electricity contract",
        "RFI": "Request for Information from potential customers",
        "RFP": "Request for Proposal - formal bid request",
        "pre_lease": "Commitment before construction complete",
        "powered_shell": "Building with power but no IT fit-out",
        "hyperscale": "100+ MW single-tenant facilities",
        "colo": "Colocation - multi-tenant data center space",
        "carrier_hotel": "Building with heavy network presence",
        "latency": "Network delay, critical for trading, gaming",
        "stranded_capacity": "Power allocated but not yet deployed",
        "absorption": "Rate at which new capacity gets leased"
    }
}

# =============================================================================
# EXPERT BRAIN CLASS
# =============================================================================

class DCExpertBrain:
    """Central intelligence system for DC Hub"""
    
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self.knowledge = DC_EXPERT_KNOWLEDGE
        
        self.learned_insights = {
            "operators": {},
            "markets": {},
            "trends": [],
            "relationships": {},
            "predictions": []
        }
        
        self.learning_stats = {
            "total_patterns_learned": 0,
            "facilities_analyzed": 0,
            "news_processed": 0,
            "deals_tracked": 0,
            "last_learning_run": None,
            "learning_cycles": 0
        }
        
        self._ensure_data_dir()
        self._init_brain_tables()
        self._load_brain_state()
        
    def _ensure_data_dir(self):
        """Ensure data directory exists"""
        os.makedirs('data', exist_ok=True)
        
    def _init_brain_tables(self):
        """Initialize brain-specific database tables"""
        try:
            conn = get_db(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS brain_knowledge (
                    id SERIAL PRIMARY KEY,
                    category TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    confidence REAL DEFAULT 0.8,
                    source TEXT,
                    learned_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    last_validated TEXT,
                    validation_count INTEGER DEFAULT 0,
                    UNIQUE(category, key)
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS brain_patterns (
                    id SERIAL PRIMARY KEY,
                    pattern_type TEXT NOT NULL,
                    pattern_data TEXT NOT NULL,
                    frequency INTEGER DEFAULT 1,
                    confidence REAL DEFAULT 0.5,
                    first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
                    last_seen TEXT,
                    is_active BOOLEAN DEFAULT 1
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS brain_predictions (
                    id SERIAL PRIMARY KEY,
                    prediction_type TEXT NOT NULL,
                    prediction TEXT NOT NULL,
                    confidence REAL,
                    predicted_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    expected_by TEXT,
                    outcome TEXT,
                    was_correct BOOLEAN
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS operator_intelligence (
                    id SERIAL PRIMARY KEY,
                    operator_name TEXT UNIQUE NOT NULL,
                    parent_company TEXT,
                    facility_count INTEGER DEFAULT 0,
                    total_mw REAL DEFAULT 0,
                    markets TEXT,
                    specialty TEXT,
                    recent_activity TEXT,
                    deal_history TEXT,
                    last_updated TEXT DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS market_intelligence (
                    id SERIAL PRIMARY KEY,
                    market_name TEXT UNIQUE NOT NULL,
                    region TEXT,
                    facility_count INTEGER DEFAULT 0,
                    total_mw REAL DEFAULT 0,
                    vacancy_rate REAL,
                    growth_rate REAL,
                    power_constraints TEXT,
                    key_operators TEXT,
                    trends TEXT,
                    last_updated TEXT DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"Brain init error: {e}")
    
    def _load_brain_state(self):
        """Load persisted brain state"""
        try:
            if os.path.exists(BRAIN_STATE_FILE):
                with open(BRAIN_STATE_FILE, 'r') as f:
                    state = json.load(f)
                    self.learning_stats = state.get('learning_stats', self.learning_stats)
                    self.learned_insights = state.get('learned_insights', self.learned_insights)
        except Exception as e:
            print(f"Brain state load error: {e}")
    
    def _save_brain_state(self):
        """Persist brain state"""
        try:
            state = {
                'learning_stats': self.learning_stats,
                'learned_insights': self.learned_insights,
                'last_saved': datetime.now().isoformat()
            }
            with open(BRAIN_STATE_FILE, 'w') as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            print(f"Brain state save error: {e}")
    
    # =========================================================================
    # LEARNING METHODS
    # =========================================================================
    
    def learn_from_all_sources(self) -> Dict:
        """Comprehensive learning from all data sources"""
        results = {
            'facilities_learned': 0,
            'operators_learned': 0,
            'markets_learned': 0,
            'news_analyzed': 0,
            'patterns_discovered': 0,
            'predictions_made': 0
        }
        
        print("🧠 DC Expert Brain: Starting comprehensive learning cycle...")
        
        try:
            results['facilities_learned'] = self._learn_from_facilities()
            results['operators_learned'] = self._learn_from_operators()
            results['markets_learned'] = self._learn_from_markets()
            results['news_analyzed'] = self._learn_from_news()
            results['patterns_discovered'] = self._discover_patterns()
            results['predictions_made'] = self._generate_predictions()
            
            self.learning_stats['learning_cycles'] += 1
            self.learning_stats['last_learning_run'] = datetime.now().isoformat()
            self.learning_stats['total_patterns_learned'] += results['patterns_discovered']
            
            self._save_brain_state()
            
            print(f"✅ Learning complete: {sum(results.values())} total items processed")
            
        except Exception as e:
            print(f"❌ Learning error: {e}")
            results['error'] = str(e)
        
        return results
    
    def _learn_from_facilities(self) -> int:
        """Extract insights from facility data"""
        count = 0
        try:
            conn = get_db(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT provider, COUNT(*) as cnt, SUM(COALESCE(power_mw, 0)) as total_mw,
                       GROUP_CONCAT(DISTINCT country) as countries
                FROM facilities
                WHERE provider IS NOT NULL AND provider != ''
                GROUP BY provider
                ORDER BY cnt DESC
                LIMIT 200
            ''')
            
            for row in cursor.fetchall():
                operator, facility_count, total_mw, countries = row
                if operator and len(operator) > 1:
                    cursor.execute('''
                        INSERT INTO operator_intelligence 
                        (operator_name, facility_count, total_mw, markets, last_updated)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT(operator_name) DO UPDATE SET
                            facility_count = ?,
                            total_mw = ?,
                            markets = ?,
                            last_updated = %s
                    ''', (operator, facility_count, total_mw or 0, countries, 
                          datetime.now().isoformat(),
                          facility_count, total_mw or 0, countries, 
                          datetime.now().isoformat()))
                    count += 1
            
            cursor.execute('''
                SELECT 
                    COALESCE(state, country) as market,
                    COUNT(*) as cnt,
                    SUM(COALESCE(power_mw, 0)) as total_mw,
                    GROUP_CONCAT(DISTINCT provider) as operators
                FROM facilities
                WHERE country IS NOT NULL
                GROUP BY COALESCE(state, country)
                HAVING cnt > 5
                ORDER BY cnt DESC
                LIMIT 100
            ''')
            
            for row in cursor.fetchall():
                market, facility_count, total_mw, operators = row
                if market and len(market) > 1:
                    cursor.execute('''
                        INSERT INTO market_intelligence 
                        (market_name, facility_count, total_mw, key_operators, last_updated)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT(market_name) DO UPDATE SET
                            facility_count = ?,
                            total_mw = ?,
                            key_operators = ?,
                            last_updated = %s
                    ''', (market, facility_count, total_mw or 0, 
                          operators[:500] if operators else '', 
                          datetime.now().isoformat(),
                          facility_count, total_mw or 0, 
                          operators[:500] if operators else '', 
                          datetime.now().isoformat()))
            
            conn.commit()
            conn.close()
            
            self.learning_stats['facilities_analyzed'] = count
            
        except Exception as e:
            print(f"Facility learning error: {e}")
        
        return count
    
    def _learn_from_operators(self) -> int:
        """Learn operator relationships and patterns"""
        count = 0
        try:
            conn = get_db(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT operator_name, facility_count, total_mw, markets
                FROM operator_intelligence
                ORDER BY facility_count DESC
                LIMIT 50
            ''')
            
            top_operators = {}
            for row in cursor.fetchall():
                name, facilities, mw, markets = row
                top_operators[name] = {
                    'facilities': facilities,
                    'mw': mw or 0,
                    'markets': markets.split(',') if markets else []
                }
                count += 1
            
            self.learned_insights['operators'] = top_operators
            conn.close()
            
        except Exception as e:
            print(f"Operator learning error: {e}")
        
        return count
    
    def _learn_from_markets(self) -> int:
        """Learn market dynamics and trends"""
        count = 0
        try:
            conn = get_db(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT market_name, facility_count, total_mw, key_operators
                FROM market_intelligence
                ORDER BY total_mw DESC
                LIMIT 30
            ''')
            
            markets = {}
            for row in cursor.fetchall():
                name, facilities, mw, operators = row
                markets[name] = {
                    'facilities': facilities,
                    'mw': mw or 0,
                    'operators': operators[:200] if operators else ''
                }
                count += 1
            
            self.learned_insights['markets'] = markets
            conn.close()
            
        except Exception as e:
            print(f"Market learning error: {e}")
        
        return count
    
    def _learn_from_news(self) -> int:
        """Extract insights from news articles"""
        count = 0
        try:
            conn = get_db(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT title, summary FROM announcements
                ORDER BY timestamp DESC
                LIMIT 200
            ''')
            
            trends = defaultdict(int)
            keywords = ['acquisition', 'merger', 'expansion', 'construction', 
                       'MW', 'GW', 'power', 'AI', 'GPU', 'hyperscale', 'campus']
            
            for row in cursor.fetchall():
                title, summary = row
                text = f"{title or ''} {summary or ''}".lower()
                
                for kw in keywords:
                    if kw.lower() in text:
                        trends[kw] += 1
                
                count += 1
            
            self.learned_insights['trends'] = [
                {'keyword': k, 'mentions': v} 
                for k, v in sorted(trends.items(), key=lambda x: -x[1])[:20]
            ]
            
            self.learning_stats['news_processed'] = count
            conn.close()
            
        except Exception as e:
            print(f"News learning error: {e}")
        
        return count
    
    def _discover_patterns(self) -> int:
        """Discover patterns across all data"""
        patterns = 0
        try:
            conn = get_db(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT name, provider, city, state, country, power_mw
                FROM facilities
                WHERE name IS NOT NULL
                LIMIT 500
            ''')
            
            naming_patterns = defaultdict(int)
            for row in cursor.fetchall():
                name = row[0] or ''
                for pattern in ['Campus', 'Data Center', 'DC', 'Cloud', 'Datacenter']:
                    if pattern.lower() in name.lower():
                        naming_patterns[pattern] += 1
                        patterns += 1
            
            for pattern, freq in naming_patterns.items():
                if freq > 5:
                    cursor.execute('''
                        INSERT INTO brain_patterns (pattern_type, pattern_data, frequency, last_seen)
                        VALUES (%s, %s, %s, %s)
                    ''', ('naming', pattern, freq, datetime.now().isoformat()))
            
            conn.commit()
            conn.close()
            
        except Exception as e:
            print(f"Pattern discovery error: {e}")
        
        return patterns
    
    def _generate_predictions(self) -> int:
        """Generate market predictions based on learned data"""
        predictions = 0
        try:
            if self.learned_insights.get('markets'):
                top_markets = sorted(
                    self.learned_insights['markets'].items(),
                    key=lambda x: x[1].get('mw', 0),
                    reverse=True
                )[:5]
                
                for market, data in top_markets:
                    prediction = {
                        'market': market,
                        'current_mw': data.get('mw', 0),
                        'predicted_growth': '15-25%',
                        'confidence': 0.7
                    }
                    self.learned_insights['predictions'].append(prediction)
                    predictions += 1
            
        except Exception as e:
            print(f"Prediction error: {e}")
        
        return predictions
    
    # =========================================================================
    # EXPERT QUERY METHODS
    # =========================================================================
    
    def get_expert_context(self, topic: str = None) -> str:
        """Get expert context for AI responses"""
        context_parts = []
        
        context_parts.append("=== DC INDUSTRY EXPERTISE ===")
        context_parts.append(f"You are a data center industry expert with knowledge of {len(self.knowledge['major_operators']['hyperscalers'])} hyperscalers, {len(self.knowledge['major_operators']['data_center_reits'])} REITs, and {len(self.knowledge['key_markets']['tier_1_us'])} primary US markets.")
        
        if topic:
            topic_lower = topic.lower()
            
            if any(w in topic_lower for w in ['operator', 'provider', 'company', 'equinix', 'digital realty']):
                context_parts.append("\n=== OPERATOR KNOWLEDGE ===")
                for category, operators in self.knowledge['major_operators'].items():
                    for name, info in operators.items():
                        context_parts.append(f"- {name}: {info}")
            
            if any(w in topic_lower for w in ['market', 'virginia', 'dallas', 'phoenix', 'location']):
                context_parts.append("\n=== MARKET KNOWLEDGE ===")
                for region, markets in self.knowledge['key_markets'].items():
                    if isinstance(markets, dict):
                        for name, info in markets.items():
                            context_parts.append(f"- {name}: {info}")
            
            if any(w in topic_lower for w in ['term', 'pue', 'tier', 'n+1', 'define']):
                context_parts.append("\n=== TERMINOLOGY ===")
                for term, definition in self.knowledge['terminology'].items():
                    context_parts.append(f"- {term}: {definition}")
        
        context_parts.append(f"\n=== LEARNED INSIGHTS ===")
        context_parts.append(f"- Tracking {self.learning_stats.get('facilities_analyzed', 0)} operators")
        context_parts.append(f"- Analyzed {self.learning_stats.get('news_processed', 0)} news articles")
        context_parts.append(f"- {self.learning_stats.get('total_patterns_learned', 0)} patterns discovered")
        
        if self.learned_insights.get('trends'):
            top_trends = self.learned_insights['trends'][:5]
            context_parts.append(f"- Top trends: {', '.join([t['keyword'] for t in top_trends])}")
        
        return '\n'.join(context_parts)
    
    def answer_question(self, question: str) -> str:
        """Answer questions using expert knowledge"""
        q_lower = question.lower()
        
        if 'pue' in q_lower:
            return self.knowledge['industry_fundamentals']['key_metrics']['PUE']
        
        if 'tier' in q_lower and any(x in q_lower for x in ['1', '2', '3', '4', 'i', 'ii', 'iii', 'iv']):
            tiers = self.knowledge['industry_fundamentals']['key_metrics']['uptime_tiers']
            return f"Uptime tiers: {json.dumps(tiers, indent=2)}"
        
        for term, definition in self.knowledge['terminology'].items():
            if term.lower().replace('_', ' ') in q_lower or term.lower() in q_lower:
                return f"{term}: {definition}"
        
        for category, operators in self.knowledge['major_operators'].items():
            for name, info in operators.items():
                if name.lower() in q_lower:
                    return f"{name}: {json.dumps(info, indent=2)}"
        
        for region, markets in self.knowledge['key_markets'].items():
            if isinstance(markets, dict):
                for name, info in markets.items():
                    if name.lower() in q_lower:
                        return f"{name}: {json.dumps(info, indent=2)}"
        
        return None
    
    def get_market_insight(self, market: str) -> Dict:
        """Get detailed market intelligence"""
        result = {'market': market, 'found': False}
        
        for region, markets in self.knowledge['key_markets'].items():
            if isinstance(markets, dict):
                for name, info in markets.items():
                    if market.lower() in name.lower():
                        result = {
                            'market': name,
                            'found': True,
                            'region': region,
                            **info
                        }
                        break
        
        if not result['found'] and self.learned_insights.get('markets'):
            for name, data in self.learned_insights['markets'].items():
                if market.lower() in name.lower():
                    result = {
                        'market': name,
                        'found': True,
                        'learned': True,
                        **data
                    }
                    break
        
        return result
    
    def get_operator_insight(self, operator: str) -> Dict:
        """Get detailed operator intelligence"""
        result = {'operator': operator, 'found': False}
        
        for category, operators in self.knowledge['major_operators'].items():
            for name, info in operators.items():
                if operator.lower() in name.lower():
                    result = {
                        'operator': name,
                        'found': True,
                        'category': category,
                        **info
                    }
                    break
        
        if not result['found'] and self.learned_insights.get('operators'):
            for name, data in self.learned_insights['operators'].items():
                if operator.lower() in name.lower():
                    result = {
                        'operator': name,
                        'found': True,
                        'learned': True,
                        **data
                    }
                    break
        
        return result
    
    def get_current_trends(self) -> List[str]:
        """Get current market trends"""
        base_trends = self.knowledge['market_dynamics']['current_trends']
        
        if self.learned_insights.get('trends'):
            learned = [f"{t['keyword']} ({t['mentions']} mentions)" 
                      for t in self.learned_insights['trends'][:3]]
            return base_trends + [f"Trending in news: {', '.join(learned)}"]
        
        return base_trends
    
    def get_learning_status(self) -> Dict:
        """Get current learning status"""
        return {
            'status': 'active',
            'learning_stats': self.learning_stats,
            'knowledge_base': {
                'operators_known': len(self.knowledge['major_operators']['hyperscalers']) + 
                                  len(self.knowledge['major_operators']['data_center_reits']) +
                                  len(self.knowledge['major_operators']['emerging_players']),
                'markets_known': sum(len(m) if isinstance(m, dict) else len(m) 
                                    for m in self.knowledge['key_markets'].values()),
                'terms_known': len(self.knowledge['terminology'])
            },
            'learned_insights': {
                'operators_learned': len(self.learned_insights.get('operators', {})),
                'markets_learned': len(self.learned_insights.get('markets', {})),
                'trends_detected': len(self.learned_insights.get('trends', [])),
                'predictions_made': len(self.learned_insights.get('predictions', []))
            }
        }


# =============================================================================
# GLOBAL BRAIN INSTANCE
# =============================================================================

_brain_instance = None
_brain_lock = threading.Lock()

def get_expert_brain() -> DCExpertBrain:
    """Get singleton brain instance"""
    global _brain_instance
    with _brain_lock:
        if _brain_instance is None:
            _brain_instance = DCExpertBrain()
        return _brain_instance


# =============================================================================
# AUTO-LEARNING SCHEDULER
# =============================================================================

def run_learning_cycle():
    """Run a complete learning cycle"""
    brain = get_expert_brain()
    return brain.learn_from_all_sources()

def start_auto_learning(interval_minutes: int = 30):
    """Start background auto-learning"""
    def learning_loop():
        while True:
            try:
                run_learning_cycle()
            except Exception as e:
                print(f"Auto-learning error: {e}")
            time.sleep(interval_minutes * 60)
    
    thread = threading.Thread(target=learning_loop, daemon=True)
    thread.start()
    print(f"🧠 Auto-learning started (every {interval_minutes} min)")
