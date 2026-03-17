"""
DC Hub Nexus - Autonomous Evolution Engine v1.0
================================================
"""

import os
import json
import sqlite3
import requests
import hashlib
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple, Any
from collections import defaultdict
from db_utils import get_db

try:
    import anthropic
    CLAUDE_AVAILABLE = True
except ImportError:
    CLAUDE_AVAILABLE = False

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

DB_PATH = 'dc_nexus.db'
EVOLUTION_LOG_PATH = 'data/evolution_log.json'
LEARNING_STATE_PATH = 'data/learning_state.json'

class EvolutionEngine:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (compatible; DCHubEvolution/1.0; +https://dchub.cloud)'
        })
        
        self.claude = None
        if CLAUDE_AVAILABLE and os.environ.get('ANTHROPIC_API_KEY'):
            self.claude = anthropic.Anthropic()
        
        self.knowledge_base = {
            'operators': {}, 'markets': {}, 'technologies': {},
            'trends': {}, 'terminology': {}, 'relationships': {}
        }
        
        self.learning_stats = {
            'total_runs': 0, 'items_learned': 0, 'improvements_made': 0,
            'apis_discovered': 0, 'quality_fixes': 0, 'last_run': None
        }
        
        self.api_sources = self._get_known_apis()
        self.pending_improvements = []
        self._init_db()
        self._load_state()
    
    def _get_known_apis(self) -> List[Dict]:
        return [
            {'name': 'PeeringDB', 'url': 'https://peeringdb.com/api', 'type': 'facility_directory',
             'endpoints': ['/fac', '/net', '/ix'], 'rate_limit': 60, 'enabled': True},
            {'name': 'OpenStreetMap/Overpass', 'url': 'https://overpass-api.de/api/interpreter',
             'type': 'geolocation', 'query_type': 'overpass_ql', 'enabled': True},
            {'name': 'Wikidata', 'url': 'https://query.wikidata.org/sparql',
             'type': 'entity_data', 'format': 'sparql', 'enabled': True},
            {'name': 'SEC EDGAR', 'url': 'https://www.sec.gov/cgi-bin/browse-edgar',
             'type': 'financial_filings', 'tickers': ['DLR', 'EQIX', 'AMT', 'CCI'], 'enabled': True},
            {'name': 'GitHub', 'url': 'https://api.github.com', 'type': 'code_search',
             'search_terms': ['data center', 'datacenter api', 'colocation'], 'enabled': True}
        ]
    
    def _init_db(self):
        conn = get_db(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''CREATE TABLE IF NOT EXISTS evolution_log (
            id SERIAL PRIMARY KEY, timestamp TIMESTAMPTZ DEFAULT NOW(),
            action_type TEXT NOT NULL, action_category TEXT, description TEXT,
            details TEXT, impact_score REAL DEFAULT 0, success BOOLEAN DEFAULT TRUE)''')
        
        cursor.execute('''CREATE TABLE IF NOT EXISTS knowledge_items (
            id SERIAL PRIMARY KEY, category TEXT NOT NULL, key TEXT NOT NULL,
            value TEXT, confidence REAL DEFAULT 0.5, source TEXT,
            learned_at TIMESTAMPTZ DEFAULT NOW(), last_updated TIMESTAMPTZ,
            usage_count INTEGER DEFAULT 0, UNIQUE(category, key))''')
        
        cursor.execute('''CREATE TABLE IF NOT EXISTS api_registry (
            id SERIAL PRIMARY KEY, name TEXT UNIQUE NOT NULL, url TEXT NOT NULL,
            api_type TEXT, documentation_url TEXT, auth_type TEXT DEFAULT 'none',
            rate_limit INTEGER, last_success TIMESTAMPTZ, error_count INTEGER DEFAULT 0,
            items_fetched INTEGER DEFAULT 0, enabled BOOLEAN DEFAULT TRUE,
            discovered_at TIMESTAMPTZ DEFAULT NOW())''')
        
        cursor.execute('''CREATE TABLE IF NOT EXISTS quality_issues (
            id SERIAL PRIMARY KEY, issue_type TEXT NOT NULL, severity TEXT DEFAULT 'low',
            entity_type TEXT, entity_id TEXT, description TEXT, suggested_fix TEXT,
            auto_fixable BOOLEAN DEFAULT FALSE, fixed BOOLEAN DEFAULT FALSE,
            discovered_at TIMESTAMPTZ DEFAULT NOW(), fixed_at TIMESTAMPTZ)''')
        
        cursor.execute('''CREATE TABLE IF NOT EXISTS feature_ideas (
            id SERIAL PRIMARY KEY, title TEXT NOT NULL, description TEXT, category TEXT,
            priority INTEGER DEFAULT 5, complexity TEXT DEFAULT 'medium',
            status TEXT DEFAULT 'proposed', source TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(), implemented_at TIMESTAMPTZ)''')
        
        cursor.execute('''CREATE TABLE IF NOT EXISTS industry_glossary (
            id SERIAL PRIMARY KEY, term TEXT UNIQUE NOT NULL, definition TEXT,
            category TEXT, related_terms TEXT, source TEXT, confidence REAL DEFAULT 0.5)''')
        
        conn.commit()
        conn.close()
    
    def _load_state(self):
        try:
            if os.path.exists(LEARNING_STATE_PATH):
                with open(LEARNING_STATE_PATH, 'r') as f:
                    state = json.load(f)
                    self.learning_stats = state.get('stats', self.learning_stats)
                    self.knowledge_base = state.get('knowledge', self.knowledge_base)
        except Exception as e:
            print(f"⚠️ Could not load learning state: {e}")
    
    def _save_state(self):
        try:
            os.makedirs('data', exist_ok=True)
            state = {'stats': self.learning_stats, 'knowledge': self.knowledge_base,
                     'last_saved': datetime.now().isoformat()}
            with open(LEARNING_STATE_PATH, 'w') as f:
                json.dump(state, f, indent=2, default=str)
        except Exception as e:
            print(f"⚠️ Could not save learning state: {e}")
    
    def _log_action(self, action_type, category, description, details=None, impact=0, success=True):
        try:
            for attempt in range(5):
                try:
                    conn = get_db(self.db_path)
                    cursor = conn.cursor()
                    cursor.execute('''INSERT INTO evolution_log 
                        (action_type, action_category, description, details, impact_score, success)
                        VALUES (%s, %s, %s, %s, %s, %s)''',
                        (action_type, category, description,
                         json.dumps(details) if details else None, impact, success))
                    conn.commit()
                    conn.close()
                    return
                except Exception as e:
                    if attempt < 4:
                        time.sleep(1.0 * (attempt + 1))
                        continue
                    print(f"⚠️ Could not log action: {e}")
                    return
        except Exception as e:
            print(f"⚠️ Could not log action: {e}")
    
    def run_evolution_cycle(self) -> Dict:
        start_time = time.time()
        results = {
            'cycle_id': datetime.now().strftime('%Y%m%d_%H%M%S'),
            'started_at': datetime.now().isoformat(),
            'phases': {}, 'total_improvements': 0, 'duration_seconds': 0
        }
        print("🧠 Evolution Engine: Starting evolution cycle...")
        results['phases']['observe'] = self._phase_observe()
        results['phases']['learn'] = self._phase_learn()
        results['phases']['analyze'] = self._phase_analyze()
        results['phases']['improve'] = self._phase_improve()
        results['phases']['validate'] = self._phase_validate()
        results['duration_seconds'] = time.time() - start_time
        results['total_improvements'] = sum(
            phase.get('improvements', 0) for phase in results['phases'].values())
        self.learning_stats['total_runs'] += 1
        self.learning_stats['last_run'] = datetime.now().isoformat()
        self._save_state()
        self._log_action('evolution_cycle', 'system',
            f"Completed evolution cycle with {results['total_improvements']} improvements",
            results, impact=results['total_improvements'] / 10.0)
        print(f"✅ Evolution cycle complete: {results['total_improvements']} improvements in {results['duration_seconds']:.1f}s")
        return results
    
    def _phase_observe(self) -> Dict:
        results = {'items_gathered': 0, 'sources_checked': 0, 'new_sources': 0}
        print("  📡 Phase 1: Observing data sources...")
        results['facility_stats'] = self._gather_facility_stats()
        results['news_stats'] = self._gather_news_stats()
        results['api_checks'] = self._check_api_health()
        results['market_signals'] = self._gather_market_signals()
        results['decisions'] = self._observe_decisions()
        results['sources_checked'] = len(self.api_sources)
        return results
    
    def _observe_decisions(self) -> Dict:
        try:
            decisions_file = 'data/decisions.json'
            if not os.path.exists(decisions_file):
                return {'total': 0, 'completed': 0, 'new_completed': 0}
            with open(decisions_file, 'r') as f:
                decisions = json.load(f)
            completed = [d for d in decisions if d.get('status') == 'completed']
            last_check = self.knowledge_base.get('decisions', {}).get('last_check')
            new_completed = [d for d in completed if d.get('completed_at', '') > last_check] if last_check else completed
            self.knowledge_base.setdefault('decisions', {})['last_check'] = datetime.now().isoformat()
            self.knowledge_base['decisions']['pending_analysis'] = new_completed
            return {'total': len(decisions), 'completed': len(completed), 'new_completed': len(new_completed)}
        except Exception as e:
            return {'total': 0, 'completed': 0, 'new_completed': 0, 'error': str(e)}
    
    def _phase_learn(self) -> Dict:
        results = {'patterns_learned': 0, 'entities_added': 0, 'relationships': 0}
        print("  🎓 Phase 2: Learning from data...")
        results['operators'] = self._learn_operators()
        results['markets'] = self._learn_markets()
        results['terminology'] = self._learn_industry_terms()
        results['deal_patterns'] = self._learn_deal_patterns()
        results['capacity_patterns'] = self._learn_capacity_patterns()
        results['decision_patterns'] = self._learn_from_decisions()
        results['patterns_learned'] = (
            results['operators'].get('new', 0) + results['markets'].get('new', 0) +
            results['terminology'].get('new', 0) + results['decision_patterns'].get('patterns_learned', 0))
        self.learning_stats['items_learned'] += results['patterns_learned']
        return results
    
    def _learn_from_decisions(self) -> Dict:
        results = {'patterns_learned': 0, 'categories_analyzed': 0, 'insights': []}
        try:
            decisions_file = 'data/decisions.json'
            if not os.path.exists(decisions_file):
                return results
            with open(decisions_file, 'r') as f:
                all_decisions = json.load(f)
            completed = [d for d in all_decisions if d.get('status') == 'completed' and d.get('outcome')]
            if not completed:
                return results
            by_category = defaultdict(list)
            by_type = defaultdict(list)
            by_technology = defaultdict(list)
            effort_accuracy = []
            for decision in completed:
                category = decision.get('category', 'other')
                dtype = decision.get('type', 'other')
                outcome = decision.get('outcome', 'unknown')
                impact = decision.get('impact_score', 0) or 0
                technologies = decision.get('technologies', [])
                effort_est = decision.get('effort_estimate')
                by_category[category].append({'outcome': outcome, 'impact': impact})
                by_type[dtype].append({'outcome': outcome, 'impact': impact})
                for tech in technologies:
                    by_technology[tech].append({'outcome': outcome, 'impact': impact})
                if effort_est and decision.get('decided_at') and decision.get('completed_at'):
                    effort_accuracy.append({
                        'estimate': effort_est,
                        'actual_duration': self._calculate_duration(decision.get('decided_at'), decision.get('completed_at'))
                    })
            decision_knowledge = {
                'category_stats': {}, 'type_stats': {}, 'technology_stats': {},
                'effort_accuracy': {}, 'last_updated': datetime.now().isoformat()
            }
            for category, decisions in by_category.items():
                total = len(decisions)
                successes = sum(1 for d in decisions if d['outcome'] == 'success')
                avg_impact = sum(d['impact'] for d in decisions) / total if total else 0
                decision_knowledge['category_stats'][category] = {
                    'total': total, 'success_rate': round(successes / total * 100, 1) if total else 0,
                    'avg_impact': round(avg_impact, 1)}
                results['categories_analyzed'] += 1
            for dtype, decisions in by_type.items():
                total = len(decisions)
                successes = sum(1 for d in decisions if d['outcome'] == 'success')
                avg_impact = sum(d['impact'] for d in decisions) / total if total else 0
                decision_knowledge['type_stats'][dtype] = {
                    'total': total, 'success_rate': round(successes / total * 100, 1) if total else 0,
                    'avg_impact': round(avg_impact, 1)}
            for tech, decisions in by_technology.items():
                total = len(decisions)
                successes = sum(1 for d in decisions if d['outcome'] == 'success')
                decision_knowledge['technology_stats'][tech] = {
                    'total': total, 'success_rate': round(successes / total * 100, 1) if total else 0}
            effort_map = {'trivial': 0.5, 'small': 2, 'medium': 8, 'large': 24, 'epic': 80}
            if effort_accuracy:
                by_estimate = defaultdict(list)
                for ea in effort_accuracy:
                    est = ea['estimate']
                    actual_hrs = ea['actual_duration'] / 3600 if ea['actual_duration'] else 0
                    expected_hrs = effort_map.get(est, 8)
                    if actual_hrs > 0:
                        by_estimate[est].append(actual_hrs / expected_hrs)
                for est, ratios in by_estimate.items():
                    avg_ratio = sum(ratios) / len(ratios) if ratios else 1
                    decision_knowledge['effort_accuracy'][est] = {
                        'samples': len(ratios), 'avg_accuracy_ratio': round(avg_ratio, 2)}
            self.knowledge_base['decisions'] = self.knowledge_base.get('decisions', {})
            self.knowledge_base['decisions']['patterns'] = decision_knowledge
            for category, stats in decision_knowledge['category_stats'].items():
                if stats['success_rate'] >= 80:
                    results['insights'].append(f"{category.title()} decisions have {stats['success_rate']}% success rate (strong area)")
                    results['patterns_learned'] += 1
                elif stats['success_rate'] <= 50 and stats['total'] >= 3:
                    results['insights'].append(f"{category.title()} decisions have only {stats['success_rate']}% success rate - needs attention")
                    results['patterns_learned'] += 1
            self._log_action('learn_decisions', 'knowledge',
                f"Learned from {len(completed)} completed decisions",
                {'categories': len(by_category), 'insights': len(results['insights'])},
                impact=len(results['insights']) * 0.5)
            print(f"    📋 Learned from {len(completed)} decisions: {results['patterns_learned']} patterns")
        except Exception as e:
            print(f"  ⚠️ Could not learn from decisions: {e}")
            results['error'] = str(e)
        return results
    
    def _calculate_duration(self, start: str, end: str) -> float:
        try:
            start_dt = datetime.fromisoformat(start.replace('Z', '+00:00'))
            end_dt = datetime.fromisoformat(end.replace('Z', '+00:00'))
            return (end_dt - start_dt).total_seconds()
        except:
            return 0
    
    def _phase_analyze(self) -> Dict:
        results = {'insights': [], 'recommendations': [], 'ai_used': False}
        print("  🔍 Phase 3: Analyzing with AI...")
        if self.claude:
            results['ai_used'] = True
            results['insights'] = self._generate_ai_insights()
            results['recommendations'] = self._generate_ai_recommendations()
            results['content_ideas'] = self._generate_content_ideas()
            results['decision_insights'] = self._analyze_decisions_with_ai()
        else:
            results['insights'] = self._generate_rule_based_insights()
        return results
    
    def _analyze_decisions_with_ai(self) -> List[Dict]:
        insights = []
        if not self.claude:
            return insights
        try:
            decision_patterns = self.knowledge_base.get('decisions', {}).get('patterns', {})
            if not decision_patterns:
                return insights
            category_stats = decision_patterns.get('category_stats', {})
            type_stats = decision_patterns.get('type_stats', {})
            tech_stats = decision_patterns.get('technology_stats', {})
            effort_acc = decision_patterns.get('effort_accuracy', {})
            if not any([category_stats, type_stats, tech_stats]):
                return insights
            prompt = f"""Analyze these development decision patterns and provide 3 actionable insights:
Decision Success Rates by Category: {json.dumps(category_stats, indent=2)}
Decision Success Rates by Type: {json.dumps(type_stats, indent=2)}
Success Rates by Technology: {json.dumps(tech_stats, indent=2)}
Effort Estimation Accuracy: {json.dumps(effort_acc, indent=2)}
Format as JSON: [{{"insight": "...", "category": "...", "action": "..."}}]"""
            response = self.claude.messages.create(
                model="claude-haiku-4-5-20241022", max_tokens=600,
                messages=[{"role": "user", "content": prompt}])
            content = response.content[0].text
            json_match = re.search(r'\[.*\]', content, re.DOTALL)
            if json_match:
                insights = json.loads(json_match.group())
                self.knowledge_base.setdefault('decisions', {})['ai_insights'] = {
                    'insights': insights, 'generated_at': datetime.now().isoformat()}
                self._log_action('analyze_decisions_ai', 'knowledge',
                    f"Generated {len(insights)} AI insights from decision patterns",
                    {'insights': insights}, impact=len(insights) * 0.3)
                print(f"    🤖 Generated {len(insights)} decision insights via Claude")
        except Exception as e:
            print(f"    ⚠️ Could not analyze decisions with AI: {e}")
            insights = [{'insight': f'Analysis pending: {str(e)[:50]}', 'category': 'system', 'action': 'retry'}]
        return insights
    
    def _phase_improve(self) -> Dict:
        results = {'improvements': 0, 'fixes': 0, 'enhancements': 0}
        print("  🔧 Phase 4: Making improvements...")
        qa_results = self._run_quality_assurance()
        results['qa'] = qa_results
        results['fixes'] = qa_results.get('auto_fixed', 0)
        enrichment = self._enrich_facility_data()
        results['enrichment'] = enrichment
        results['enhancements'] += enrichment.get('enhanced', 0)
        new_sources = self._discover_new_sources()
        results['new_sources'] = new_sources
        results['improvements'] += new_sources.get('added', 0)
        results['improvements'] += results['fixes'] + results['enhancements']
        self.learning_stats['improvements_made'] += results['improvements']
        self.learning_stats['quality_fixes'] += results['fixes']
        return results
    
    def _phase_validate(self) -> Dict:
        results = {'health_score': 0, 'issues': [], 'validations': 0}
        print("  ✓ Phase 5: Validating system health...")
        results['data_quality'] = self._validate_data_quality()
        results['api_health'] = self._validate_api_health()
        results['coverage'] = self._validate_coverage()
        scores = [results['data_quality'].get('score', 0), results['api_health'].get('score', 0),
                  results['coverage'].get('score', 0)]
        results['health_score'] = sum(scores) / len(scores) if scores else 0
        return results
    
    def _gather_facility_stats(self) -> Dict:
        try:
            conn = get_db(self.db_path)
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM facilities')
            total = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(DISTINCT provider) FROM facilities WHERE provider IS NOT NULL')
            operators = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(DISTINCT country) FROM facilities WHERE country IS NOT NULL')
            countries = cursor.fetchone()[0]
            # FIX: cast last_updated text to timestamptz for PostgreSQL
            cursor.execute("""SELECT COUNT(*) FROM facilities 
                WHERE last_updated::timestamptz > (NOW() - INTERVAL '7 days')""")
            recent = cursor.fetchone()[0]
            conn.close()
            return {'total_facilities': total, 'operators': operators,
                    'countries': countries, 'updated_last_week': recent}
        except Exception as e:
            return {'error': str(e)}
    
    def _gather_news_stats(self) -> Dict:
        try:
            conn = get_db(self.db_path)
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM announcements')
            total = cursor.fetchone()[0]
            # FIX: cast published_date text to timestamptz
            cursor.execute("""SELECT COUNT(*) FROM announcements 
                WHERE published_date::timestamptz > (NOW() - INTERVAL '24 hours')""")
            today = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(DISTINCT source) FROM announcements')
            sources = cursor.fetchone()[0]
            conn.close()
            return {'total_articles': total, 'today': today, 'unique_sources': sources}
        except Exception as e:
            return {'error': str(e)}
    
    def _check_api_health(self) -> List[Dict]:
        health_results = []
        for api in self.api_sources[:5]:
            try:
                response = self.session.get(api['url'], timeout=10)
                health_results.append({
                    'name': api['name'],
                    'status': 'healthy' if response.status_code < 400 else 'unhealthy',
                    'response_time': response.elapsed.total_seconds()})
            except:
                health_results.append({'name': api['name'], 'status': 'unreachable', 'response_time': None})
        return health_results
    
    def _gather_market_signals(self) -> Dict:
        signals = {'deal_mentions': 0, 'expansion_mentions': 0, 'hot_markets': []}
        try:
            conn = get_db(self.db_path)
            cursor = conn.cursor()
            # FIX: cast published_date text to timestamptz
            cursor.execute("""SELECT title FROM announcements 
                WHERE published_date::timestamptz > (NOW() - INTERVAL '7 days') LIMIT 100""")
            for (title,) in cursor.fetchall():
                title_lower = title.lower() if title else ''
                if any(kw in title_lower for kw in ['acquire', 'merger', 'deal', 'buy', 'invest']):
                    signals['deal_mentions'] += 1
                if any(kw in title_lower for kw in ['expand', 'new', 'build', 'develop', 'mw', 'megawatt']):
                    signals['expansion_mentions'] += 1
            # FIX: cast last_updated text to timestamptz
            cursor.execute("""SELECT city, COUNT(*) as cnt FROM facilities 
                WHERE last_updated::timestamptz > (NOW() - INTERVAL '30 days')
                GROUP BY city ORDER BY cnt DESC LIMIT 5""")
            signals['hot_markets'] = [row[0] for row in cursor.fetchall() if row[0]]
            conn.close()
        except Exception as e:
            signals['error'] = str(e)
        return signals
    
    def _learn_operators(self) -> Dict:
        results = {'total': 0, 'new': 0}
        try:
            conn = get_db(self.db_path)
            cursor = conn.cursor()
            # FIX: GROUP_CONCAT → STRING_AGG for PostgreSQL
            cursor.execute("""SELECT provider, COUNT(*) as cnt, 
                STRING_AGG(DISTINCT city, ',') as cities,
                STRING_AGG(DISTINCT country, ',') as countries
                FROM facilities 
                WHERE provider IS NOT NULL AND provider != ''
                GROUP BY provider ORDER BY cnt DESC LIMIT 200""")
            for row in cursor.fetchall():
                provider, count, cities, countries = row
                cursor.execute('''INSERT INTO knowledge_items (category, key, value, confidence, source)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT(category, key) DO UPDATE SET
                        value = EXCLUDED.value,
                        usage_count = knowledge_items.usage_count + 1,
                        last_updated = NOW()''',
                    ('operators', provider,
                     json.dumps({'facilities': count, 'cities': cities, 'countries': countries}),
                     min(count / 100.0, 1.0), 'facility_analysis'))
                if provider not in self.knowledge_base['operators']:
                    self.knowledge_base['operators'][provider] = {'facilities': count}
                    results['new'] += 1
                results['total'] += 1
            conn.commit()
            conn.close()
        except Exception as e:
            results['error'] = str(e)
        return results
    
    def _learn_markets(self) -> Dict:
        results = {'total': 0, 'new': 0}
        try:
            conn = get_db(self.db_path)
            cursor = conn.cursor()
            # FIX: HAVING cnt → HAVING COUNT(*) for PostgreSQL
            cursor.execute("""SELECT city, state, country, COUNT(*) as cnt,
                SUM(CASE WHEN power_mw IS NOT NULL THEN power_mw ELSE 0 END) as total_mw
                FROM facilities WHERE city IS NOT NULL
                GROUP BY city, state, country
                HAVING COUNT(*) >= 3
                ORDER BY cnt DESC LIMIT 100""")
            for row in cursor.fetchall():
                city, state, country, count, total_mw = row
                market_key = f"{city}, {state or country}"
                cursor.execute('''INSERT INTO knowledge_items (category, key, value, confidence, source)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT(category, key) DO UPDATE SET
                        value = EXCLUDED.value,
                        usage_count = knowledge_items.usage_count + 1,
                        last_updated = NOW()''',
                    ('markets', market_key,
                     json.dumps({'facilities': count, 'total_mw': total_mw or 0}),
                     min(count / 50.0, 1.0), 'market_analysis'))
                if market_key not in self.knowledge_base['markets']:
                    results['new'] += 1
                results['total'] += 1
            conn.commit()
            conn.close()
        except Exception as e:
            results['error'] = str(e)
        return results
    
    def _learn_industry_terms(self) -> Dict:
        results = {'total': 0, 'new': 0}
        industry_terms = {
            'colocation': 'Shared data center facility where multiple customers lease space',
            'hyperscale': 'Extremely large data center, typically 100MW+ for cloud/tech giants',
            'edge': 'Smaller facilities located closer to end users for low latency',
            'PUE': 'Power Usage Effectiveness - ratio of total facility power to IT equipment power',
            'tier': 'Uptime Institute classification of data center redundancy (I-IV)',
            'N+1': 'Redundancy design with one backup for N primary components',
            'hot aisle': 'Row of server racks where hot exhaust air is expelled',
            'cold aisle': 'Row of server racks where cool air is supplied',
            'UPS': 'Uninterruptible Power Supply - battery backup for power continuity',
            'PDU': 'Power Distribution Unit - distributes power to server racks',
            'CRAH': 'Computer Room Air Handler - precision cooling unit',
            'CRAC': 'Computer Room Air Conditioner - precision cooling unit',
            'raised floor': 'Elevated floor with space below for cooling air distribution',
            'cross-connect': 'Direct connection between two tenants in the same facility',
            'meet-me room': 'Carrier-neutral room where network providers interconnect',
            'MW': 'Megawatt - unit of power capacity (1MW = 1000kW)',
            'shell': 'Empty building ready for data center fit-out',
            'white space': 'Usable data center floor space for IT equipment',
            'dark fiber': 'Unused fiber optic cable capacity',
            'latency': 'Time delay in data transmission, measured in milliseconds',
            'interconnection': 'Physical connection between networks at a data center',
            'IX': 'Internet Exchange point where networks peer',
            'REIT': 'Real Estate Investment Trust - tax-advantaged real estate company',
            'cap rate': 'Capitalization rate - property value metric for investment',
            'NNN': 'Triple net lease where tenant pays taxes, insurance, maintenance',
            'powered shell': 'Building with power infrastructure but no IT equipment',
            'critical load': 'Essential power load for IT equipment',
            'mechanical load': 'Power for cooling and support systems',
            'carrier hotel': 'Building with many network providers and interconnections',
            'modular': 'Pre-fabricated data center components for rapid deployment'
        }
        try:
            conn = get_db(self.db_path)
            cursor = conn.cursor()
            for term, definition in industry_terms.items():
                # FIX: MAX(industry_glossary.confidence, 0.9) → GREATEST with explicit casts
                cursor.execute('''INSERT INTO industry_glossary (term, definition, category, confidence)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT(term) DO UPDATE SET
                        definition = EXCLUDED.definition,
                        confidence = GREATEST(industry_glossary.confidence::numeric, 0.9::numeric)''',
                    (term.lower(), definition, 'general', 0.95))
                if term.lower() not in self.knowledge_base['terminology']:
                    self.knowledge_base['terminology'][term.lower()] = definition
                    results['new'] += 1
                results['total'] += 1
            conn.commit()
            conn.close()
        except Exception as e:
            results['error'] = str(e)
        return results
    
    def _learn_deal_patterns(self) -> Dict:
        results = {'patterns': 0}
        deal_keywords = ['acquire', 'acquisition', 'merger', 'buy', 'purchase', 'invest',
            'investment', 'funding', 'stake', 'equity', 'joint venture', 'jv', 'partnership',
            'alliance', 'recapitalization', 'recap', 'refinance', 'ipo', 'divestiture',
            'sell', 'divest', 'spin-off']
        for keyword in deal_keywords:
            self._learn_entity('deal_keywords', keyword, 'predefined', 0.9)
            results['patterns'] += 1
        return results
    
    def _learn_capacity_patterns(self) -> Dict:
        results = {'patterns': 0}
        try:
            conn = get_db(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""SELECT title, summary FROM announcements
                WHERE title LIKE '%MW%' OR summary LIKE '%MW%'
                ORDER BY published_date DESC LIMIT 100""")
            for title, summary in cursor.fetchall():
                text = f"{title or ''} {summary or ''}"
                mw_match = re.search(r'(\d+(?:\.\d+)?)\s*MW', text, re.IGNORECASE)
                if mw_match:
                    results['patterns'] += 1
            conn.close()
        except Exception as e:
            results['error'] = str(e)
        return results
    
    def _learn_entity(self, entity_type: str, value: str, source: str, confidence: float = 0.5) -> bool:
        try:
            conn = get_db(self.db_path)
            cursor = conn.cursor()
            # FIX: MAX(knowledge_items.confidence, ?) → GREATEST with explicit numeric casts
            cursor.execute('''INSERT INTO knowledge_items (category, key, confidence, source)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(category, key) DO UPDATE SET
                    confidence = GREATEST(knowledge_items.confidence::numeric, %s::numeric),
                    usage_count = knowledge_items.usage_count + 1,
                    last_updated = NOW()''',
                (entity_type, value, confidence, source, confidence))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            return False
    
    def _generate_ai_insights(self) -> List[Dict]:
        insights = []
        if not self.claude:
            return insights
        try:
            conn = get_db(self.db_path)
            cursor = conn.cursor()
            cursor.execute('SELECT title FROM announcements ORDER BY published_date DESC LIMIT 20')
            recent_news = [row[0] for row in cursor.fetchall() if row[0]]
            cursor.execute('SELECT provider, COUNT(*) FROM facilities GROUP BY provider ORDER BY COUNT(*) DESC LIMIT 10')
            top_operators = [row[0] for row in cursor.fetchall() if row[0]]
            conn.close()
            prompt = f"""Analyze these recent data center industry headlines and provide 3 brief insights:
Recent Headlines: {chr(10).join(recent_news[:15])}
Top Operators: {', '.join(top_operators[:5])}
Format as JSON: [{{"insight": "...", "category": "..."}}]"""
            response = self.claude.messages.create(model="claude-haiku-4-5-20241022", max_tokens=500,
                messages=[{"role": "user", "content": prompt}])
            content = response.content[0].text
            json_match = re.search(r'\[.*\]', content, re.DOTALL)
            if json_match:
                insights = json.loads(json_match.group())
        except Exception as e:
            insights = [{'insight': f'Analysis pending: {str(e)[:50]}', 'category': 'system'}]
        return insights
    
    def _generate_ai_recommendations(self) -> List[str]:
        if not self.claude:
            return []
        try:
            stats = self._gather_facility_stats()
            news_stats = self._gather_news_stats()
            prompt = f"""Suggest 3 specific improvements for a data center intelligence platform.
Current Stats: {stats.get('total_facilities', 0)} facilities, {stats.get('operators', 0)} operators,
{news_stats.get('total_articles', 0)} news articles, {news_stats.get('unique_sources', 0)} sources.
Return as JSON: [{{"improvement": "...", "priority": "high/medium/low"}}]"""
            response = self.claude.messages.create(model="claude-haiku-4-5-20241022", max_tokens=500,
                messages=[{"role": "user", "content": prompt}])
            content = response.content[0].text
            json_match = re.search(r'\[.*\]', content, re.DOTALL)
            if json_match:
                recommendations = json.loads(json_match.group())
                return [r['improvement'] for r in recommendations]
        except:
            pass
        return []
    
    def _generate_content_ideas(self) -> List[Dict]:
        ideas = []
        try:
            signals = self._gather_market_signals()
            if signals.get('hot_markets'):
                for market in signals['hot_markets'][:3]:
                    ideas.append({'type': 'market_report', 'title': f'{market} Data Center Market Analysis',
                                  'reason': 'High activity detected'})
            if signals.get('deal_mentions', 0) > 5:
                ideas.append({'type': 'deal_roundup', 'title': 'Weekly M&A and Investment Roundup',
                              'reason': f"{signals['deal_mentions']} deal mentions this week"})
        except:
            pass
        return ideas
    
    def _generate_rule_based_insights(self) -> List[Dict]:
        insights = []
        stats = self._gather_facility_stats()
        signals = self._gather_market_signals()
        if signals.get('deal_mentions', 0) > 3:
            insights.append({'insight': f"M&A activity elevated with {signals['deal_mentions']} deal mentions this week", 'category': 'deals'})
        if signals.get('expansion_mentions', 0) > 5:
            insights.append({'insight': f"Strong expansion activity with {signals['expansion_mentions']} new development mentions", 'category': 'capacity'})
        if signals.get('hot_markets'):
            insights.append({'insight': f"Hot markets this month: {', '.join(signals['hot_markets'][:3])}", 'category': 'markets'})
        return insights
    
    def _run_quality_assurance(self) -> Dict:
        results = {'issues_found': 0, 'auto_fixed': 0, 'pending_review': 0}
        try:
            conn = get_db(self.db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT id, name FROM facilities WHERE name IS NOT NULL AND name != '' AND LENGTH(name) < 3")
            for fid, name in cursor.fetchall():
                self._log_quality_issue('short_name', 'facility', str(fid), f"Name too short: '{name}'", auto_fixable=False)
                results['issues_found'] += 1
            cursor.execute("SELECT id, name FROM facilities WHERE name IS NOT NULL AND (name ILIKE '%test%' OR name ILIKE '%sample%' OR name ILIKE '%example%')")
            for fid, name in cursor.fetchall():
                self._log_quality_issue('test_data', 'facility', str(fid), f"Possible test data: '{name}'", auto_fixable=True)
                results['issues_found'] += 1
            cursor.execute("SELECT id FROM facilities WHERE provider IS NULL OR provider = ''")
            missing_provider = len(cursor.fetchall())
            if missing_provider > 0:
                self._log_quality_issue('missing_data', 'facility', 'multiple', f"{missing_provider} facilities missing operator", auto_fixable=False)
                results['issues_found'] += 1
            conn.close()
        except Exception as e:
            results['error'] = str(e)
        return results
    
    def _log_quality_issue(self, issue_type, entity_type, entity_id, description, auto_fixable=False):
        try:
            conn = get_db(self.db_path)
            cursor = conn.cursor()
            cursor.execute('INSERT INTO quality_issues (issue_type, entity_type, entity_id, description, auto_fixable) VALUES (%s, %s, %s, %s, %s)',
                (issue_type, entity_type, entity_id, description, auto_fixable))
            conn.commit()
            conn.close()
        except:
            pass
    
    def _enrich_facility_data(self) -> Dict:
        return {'enhanced': 0, 'checked': 0}
    
    def _discover_new_sources(self) -> Dict:
        results = {'checked': 0, 'added': 0}
        potential_apis = [
            {'name': 'Cloudscene API', 'url': 'https://cloudscene.com/api', 'type': 'directory'},
            {'name': 'DataCenterMap', 'url': 'https://www.datacentermap.com', 'type': 'directory'},
        ]
        for api in potential_apis:
            results['checked'] += 1
            try:
                response = self.session.head(api['url'], timeout=60)
                if response.status_code < 400:
                    self._register_api_source(api['name'], api['url'], api['type'])
                    results['added'] += 1
            except:
                pass
        return results
    
    def _register_api_source(self, name, url, api_type):
        try:
            conn = get_db(self.db_path)
            cursor = conn.cursor()
            cursor.execute('INSERT INTO api_registry (name, url, api_type) VALUES (%s, %s, %s) ON CONFLICT(name) DO NOTHING',
                (name, url, api_type))
            conn.commit()
            conn.close()
        except:
            pass
    
    def _validate_data_quality(self) -> Dict:
        try:
            conn = get_db(self.db_path)
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM facilities')
            total = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM facilities WHERE name IS NOT NULL AND provider IS NOT NULL AND (city IS NOT NULL OR country IS NOT NULL)")
            complete = cursor.fetchone()[0]
            conn.close()
            completeness = (complete / total * 100) if total > 0 else 0
            return {'total_records': total, 'complete_records': complete,
                    'completeness': round(completeness, 1), 'score': completeness / 100}
        except Exception as e:
            return {'score': 0, 'error': str(e)}
    
    def _validate_api_health(self) -> Dict:
        healthy = sum(1 for api in self.api_sources if api.get('enabled', False))
        total = len(self.api_sources)
        return {'healthy_apis': healthy, 'total_apis': total, 'score': healthy / total if total > 0 else 0}
    
    def _validate_coverage(self) -> Dict:
        try:
            conn = get_db(self.db_path)
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(DISTINCT country) FROM facilities WHERE country IS NOT NULL')
            countries = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(DISTINCT city) FROM facilities WHERE city IS NOT NULL')
            cities = cursor.fetchone()[0]
            conn.close()
            coverage_score = min((countries / 50.0 + cities / 500.0) / 2, 1.0)
            return {'countries': countries, 'cities': cities, 'score': coverage_score}
        except Exception as e:
            return {'score': 0, 'error': str(e)}
    
    def teach_industry_knowledge(self, topic: str) -> Dict:
        if not self.claude:
            return {'error': 'Claude AI not available', 'topic': topic}
        try:
            prompt = f"""You are a data center industry expert. Teach me about: "{topic}"
Provide: definition, key players, market trends, important metrics, related technologies.
Format as JSON: {{"topic": "{topic}", "definition": "...", "key_players": [], "trends": [], "metrics": {{}}, "related_topics": []}}"""
            response = self.claude.messages.create(model="claude-haiku-4-5-20241022", max_tokens=1000,
                messages=[{"role": "user", "content": prompt}])
            content = response.content[0].text
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if json_match:
                knowledge = json.loads(json_match.group())
                self._learn_entity('topics', topic, 'ai_teaching', 0.8)
                for player in knowledge.get('key_players', []):
                    self._learn_entity('operators', player, 'ai_teaching', 0.7)
                self._log_action('teach', 'knowledge', f"Learned about: {topic}", knowledge, 0.5)
                return knowledge
        except Exception as e:
            return {'error': str(e), 'topic': topic}
        return {'topic': topic, 'status': 'learning_failed'}
    
    def get_learning_status(self) -> Dict:
        try:
            conn = get_db(self.db_path)
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM knowledge_items')
            knowledge_count = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM evolution_log')
            actions_count = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM quality_issues WHERE fixed = FALSE")
            open_issues = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM api_registry WHERE enabled = TRUE")
            active_apis = cursor.fetchone()[0]
            # FIX: cast timestamp text to timestamptz
            cursor.execute("""SELECT action_type, COUNT(*) FROM evolution_log 
                WHERE timestamp::timestamptz > (NOW() - INTERVAL '24 hours') GROUP BY action_type""")
            recent_actions = dict(cursor.fetchall())
            conn.close()
            return {
                'stats': self.learning_stats, 'knowledge_items': knowledge_count,
                'total_actions': actions_count, 'open_quality_issues': open_issues,
                'active_api_sources': active_apis, 'recent_actions_24h': recent_actions,
                'knowledge_categories': list(self.knowledge_base.keys()), 'ai_enabled': self.claude is not None
            }
        except Exception as e:
            return {'error': str(e), 'stats': self.learning_stats}
    
    def suggest_next_improvements(self) -> List[Dict]:
        suggestions = []
        status = self.get_learning_status()
        if status.get('open_quality_issues', 0) > 10:
            suggestions.append({'type': 'quality', 'priority': 'high',
                'suggestion': f"Address {status['open_quality_issues']} open quality issues", 'action': 'run_quality_assurance'})
        if status.get('active_api_sources', 0) < 5:
            suggestions.append({'type': 'data_sources', 'priority': 'medium',
                'suggestion': 'Discover and integrate more data sources', 'action': 'discover_new_sources'})
        if status.get('knowledge_items', 0) < 100:
            suggestions.append({'type': 'knowledge', 'priority': 'medium',
                'suggestion': 'Build more industry knowledge', 'action': 'teach_industry_knowledge'})
        if self.claude:
            suggestions.append({'type': 'content', 'priority': 'low',
                'suggestion': 'Generate market intelligence content', 'action': 'generate_content_ideas'})
        return suggestions


_evolution_engine = None

def get_evolution_engine() -> EvolutionEngine:
    global _evolution_engine
    if _evolution_engine is None:
        _evolution_engine = EvolutionEngine()
    return _evolution_engine

def run_evolution_cycle() -> Dict:
    return get_evolution_engine().run_evolution_cycle()

def get_learning_status() -> Dict:
    return get_evolution_engine().get_learning_status()

def teach_topic(topic: str) -> Dict:
    return get_evolution_engine().teach_industry_knowledge(topic)


if __name__ == '__main__':
    print("🧠 DC Hub Nexus - Evolution Engine")
    print("=" * 50)
    engine = EvolutionEngine()
    print("\n📊 Current Status:")
    status = engine.get_learning_status()
    print(json.dumps(status, indent=2, default=str))
    print("\n🔄 Running Evolution Cycle...")
    results = engine.run_evolution_cycle()
    print(json.dumps(results, indent=2, default=str))
    print("\n💡 Suggested Improvements:")
    for s in engine.suggest_next_improvements():
        print(f"  [{s['priority'].upper()}] {s['suggestion']}")
