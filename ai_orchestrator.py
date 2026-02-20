"""
DC Hub Nexus - AI Orchestrator v1.0
====================================
The "Master Brain" that coordinates all AI agents and enables proactive behaviors.

PROACTIVE CAPABILITIES:
1. Market Pulse - Real-time monitoring with anomaly detection
2. Predictive Intelligence - Trend forecasting and deal prediction
3. Cross-Agent Learning - Shares insights between all agents
4. Opportunity Hunter - Proactively identifies investment/deal opportunities
5. Smart Alerting - Context-aware notifications based on significance
6. Auto-Enhancement - Self-improves based on data patterns

COORDINATION:
- Orchestrates: Evolution Engine, Deep Learning, Global Intelligence, Proactive Discovery
- Shares: Knowledge base, patterns, market signals across all agents
- Prioritizes: Tasks based on impact and urgency
"""

import os
import json
import sqlite3
import threading
import time
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple, Any
from collections import defaultdict
import hashlib
from db_utils import get_db

try:
    import anthropic
    CLAUDE_AVAILABLE = True
except ImportError:
    CLAUDE_AVAILABLE = False

DB_PATH = 'dc_nexus.db'
ORCHESTRATOR_STATE_PATH = 'data/orchestrator_state.json'

KNOWN_OPERATORS = {
    'equinix': 'Equinix', 'digital realty': 'Digital Realty', 'qts': 'QTS',
    'cyrusone': 'CyrusOne', 'coresite': 'CoreSite', 'flexential': 'Flexential',
    'vantage': 'Vantage', 'stack': 'Stack Infrastructure', 'databank': 'DataBank',
    'compass': 'Compass Datacenters', 'applied digital': 'Applied Digital',
    'crusoe': 'Crusoe Energy', 'lancium': 'Lancium', 'dayone': 'DayOne',
    'nebius': 'Nebius', 'xai': 'xAI', 'microsoft': 'Microsoft', 'google': 'Google',
    'amazon': 'Amazon/AWS', 'meta': 'Meta', 'oracle': 'Oracle', 'cloudflare': 'Cloudflare',
    'ntt': 'NTT', 'lumen': 'Lumen', 'cologix': 'Cologix', 'sabey': 'Sabey',
    'aligned': 'Aligned Data Centers', 'switch': 'Switch', 'prime': 'Prime Data Centers',
    'yondr': 'Yondr Group', 'edgeconnex': 'EdgeConneX', 'scala': 'Scala Data Centers'
}

MARKET_INDICATORS = {
    'bullish': ['expansion', 'growth', 'new campus', 'groundbreaking', 'investment', 
                'pipeline', 'development', 'breaking ground', 'new facility'],
    'bearish': ['delay', 'cancel', 'postpone', 'halt', 'suspend', 'downturn',
                'vacancy', 'oversupply', 'slowdown'],
    'deal_signals': ['acquire', 'merger', 'acquisition', 'buy', 'sell', 'stake',
                     'investment', 'funding', 'joint venture', 'partnership'],
    'capacity_signals': ['MW', 'megawatt', 'GW', 'gigawatt', 'capacity', 'power',
                         'hyperscale', 'campus', 'phase', 'expansion']
}

EMERGING_MARKETS = [
    'Columbus', 'Nashville', 'Kansas City', 'Salt Lake City', 'Portland',
    'Minneapolis', 'Indianapolis', 'Austin', 'San Antonio', 'Memphis',
    'Jakarta', 'Johor', 'Ho Chi Minh', 'Bangkok', 'Manila',
    'Warsaw', 'Madrid', 'Milan', 'Stockholm', 'Helsinki',
    'Johannesburg', 'Cairo', 'Lagos', 'Nairobi', 'Cape Town'
]


class AIOrchestrator:
    """Master brain coordinating all AI agents with proactive intelligence."""
    
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self.claude = None
        if CLAUDE_AVAILABLE and os.environ.get('ANTHROPIC_API_KEY'):
            self.claude = anthropic.Anthropic()
        
        self.market_pulse = {
            'sentiment': 'neutral',
            'sentiment_score': 0.5,
            'trending_operators': [],
            'trending_markets': [],
            'hot_deals': [],
            'capacity_momentum': 0,
            'last_analysis': None
        }
        
        self.predictions = {
            'next_deals': [],
            'capacity_forecast': {},
            'emerging_hotspots': [],
            'risk_alerts': [],
            'last_updated': None
        }
        
        self.opportunity_queue = []
        self.alert_history = []
        self.cross_agent_insights = {}
        
        self.stats = {
            'orchestrations': 0,
            'predictions_made': 0,
            'opportunities_found': 0,
            'alerts_generated': 0,
            'patterns_shared': 0,
            'ai_calls': 0,
            'last_run': None
        }
        
        self._init_db()
        self._load_state()
    
    def _init_db(self):
        """Initialize orchestrator tables"""
        conn = get_db(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ai_predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prediction_type TEXT NOT NULL,
                prediction_data TEXT NOT NULL,
                confidence REAL DEFAULT 0.5,
                timeframe TEXT,
                status TEXT DEFAULT 'pending',
                outcome TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                evaluated_at TEXT
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS market_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_type TEXT NOT NULL,
                signal_data TEXT NOT NULL,
                source TEXT,
                strength REAL DEFAULT 0.5,
                detected_at TEXT DEFAULT CURRENT_TIMESTAMP,
                processed BOOLEAN DEFAULT 0
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS opportunity_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_type TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                entities TEXT,
                priority INTEGER DEFAULT 5,
                status TEXT DEFAULT 'new',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                actioned_at TEXT
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS agent_knowledge_share (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_agent TEXT NOT NULL,
                to_agent TEXT,
                insight_type TEXT NOT NULL,
                insight_data TEXT NOT NULL,
                confidence REAL DEFAULT 0.5,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                consumed BOOLEAN DEFAULT 0
            )
        ''')
        
        conn.commit()
        conn.close()
    
    def _load_state(self):
        """Load orchestrator state from file"""
        os.makedirs('data', exist_ok=True)
        if os.path.exists(ORCHESTRATOR_STATE_PATH):
            try:
                with open(ORCHESTRATOR_STATE_PATH, 'r') as f:
                    state = json.load(f)
                    self.market_pulse.update(state.get('market_pulse', {}))
                    self.predictions.update(state.get('predictions', {}))
                    self.stats.update(state.get('stats', {}))
            except Exception as e:
                print(f"[Orchestrator] Failed to load state: {e}")
    
    def _save_state(self):
        """Save orchestrator state to file"""
        try:
            state = {
                'market_pulse': self.market_pulse,
                'predictions': self.predictions,
                'stats': self.stats,
                'saved_at': datetime.now().isoformat()
            }
            with open(ORCHESTRATOR_STATE_PATH, 'w') as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            print(f"[Orchestrator] Failed to save state: {e}")
    
    def analyze_market_pulse(self) -> Dict:
        """Analyze current market sentiment and trends from all data sources."""
        print(f"\n{'='*60}")
        print(f"[{datetime.now().strftime('%H:%M:%S')}] MARKET PULSE ANALYSIS")
        print(f"{'='*60}")
        
        conn = get_db(self.db_path)
        cursor = conn.cursor()
        
        bullish_count = 0
        bearish_count = 0
        operator_mentions = defaultdict(int)
        market_mentions = defaultdict(int)
        recent_deals = []
        capacity_pipeline = 0
        
        try:
            cursor.execute('''
                SELECT title, summary, source FROM announcements 
                WHERE timestamp > datetime('now', '-7 days')
                ORDER BY timestamp DESC LIMIT 200
            ''')
            news = cursor.fetchall()
            
            for title, summary, source in news:
                text = f"{title or ''} {summary or ''}".lower()
                
                for indicator in MARKET_INDICATORS['bullish']:
                    if indicator in text:
                        bullish_count += 1
                
                for indicator in MARKET_INDICATORS['bearish']:
                    if indicator in text:
                        bearish_count += 1
                
                for key, name in KNOWN_OPERATORS.items():
                    if key in text:
                        operator_mentions[name] += 1
                
                for market in EMERGING_MARKETS:
                    if market.lower() in text:
                        market_mentions[market] += 1
                
                if any(sig in text for sig in MARKET_INDICATORS['deal_signals']):
                    recent_deals.append({
                        'headline': title[:100] if title else '',
                        'source': source
                    })
            
            cursor.execute('''
                SELECT SUM(mw) FROM capacity_pipeline 
                WHERE status != 'cancelled'
            ''')
            result = cursor.fetchone()
            capacity_pipeline = result[0] if result and result[0] else 0
            
        except Exception as e:
            print(f"  [!] Error analyzing data: {e}")
        finally:
            conn.close()
        
        total_signals = bullish_count + bearish_count
        if total_signals > 0:
            sentiment_score = bullish_count / total_signals
            if sentiment_score > 0.65:
                sentiment = 'bullish'
            elif sentiment_score < 0.35:
                sentiment = 'bearish'
            else:
                sentiment = 'neutral'
        else:
            sentiment = 'neutral'
            sentiment_score = 0.5
        
        trending_operators = sorted(operator_mentions.items(), key=lambda x: -x[1])[:10]
        trending_markets = sorted(market_mentions.items(), key=lambda x: -x[1])[:10]
        
        self.market_pulse = {
            'sentiment': sentiment,
            'sentiment_score': round(sentiment_score, 2),
            'bullish_signals': bullish_count,
            'bearish_signals': bearish_count,
            'trending_operators': [{'name': k, 'mentions': v} for k, v in trending_operators],
            'trending_markets': [{'name': k, 'mentions': v} for k, v in trending_markets],
            'hot_deals': recent_deals[:5],
            'capacity_pipeline_mw': capacity_pipeline,
            'capacity_momentum': self._calculate_momentum(capacity_pipeline),
            'last_analysis': datetime.now().isoformat()
        }
        
        print(f"  Sentiment: {sentiment.upper()} (score: {sentiment_score:.2f})")
        print(f"  Bullish signals: {bullish_count}, Bearish: {bearish_count}")
        print(f"  Top operator: {trending_operators[0][0] if trending_operators else 'N/A'}")
        print(f"  Pipeline: {capacity_pipeline:,.0f} MW")
        
        self._save_state()
        return self.market_pulse
    
    def _calculate_momentum(self, current_pipeline: float) -> str:
        """Calculate capacity pipeline momentum"""
        if current_pipeline > 10000:
            return 'accelerating'
        elif current_pipeline > 5000:
            return 'strong'
        elif current_pipeline > 2000:
            return 'moderate'
        else:
            return 'slow'
    
    def generate_predictions(self) -> Dict:
        """Generate AI-powered predictions for market, deals, and capacity."""
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] GENERATING PREDICTIONS")
        
        predictions = {
            'next_deals': [],
            'capacity_forecast': {},
            'emerging_hotspots': [],
            'risk_alerts': [],
            'confidence_overall': 0.5
        }
        
        if self.claude:
            predictions = self._generate_ai_predictions()
        else:
            predictions = self._generate_rule_predictions()
        
        predictions['last_updated'] = datetime.now().isoformat()
        self.predictions = predictions
        self.stats['predictions_made'] += 1
        
        self._store_predictions(predictions)
        self._save_state()
        
        return predictions
    
    def _generate_ai_predictions(self) -> Dict:
        """Use Claude for sophisticated predictions."""
        try:
            context = json.dumps({
                'market_pulse': self.market_pulse,
                'trending_operators': self.market_pulse.get('trending_operators', [])[:5],
                'trending_markets': self.market_pulse.get('trending_markets', [])[:5],
                'capacity_pipeline': self.market_pulse.get('capacity_pipeline_mw', 0)
            })
            
            prompt = f"""Based on this data center market data, generate predictions:
{context}

Return JSON with:
1. next_deals: 3 most likely M&A deals (buyer, target, confidence 0-1, rationale)
2. capacity_forecast: Top 5 markets and expected MW growth next 12 months
3. emerging_hotspots: 3 markets about to boom (name, why, confidence)
4. risk_alerts: 2 market risks to watch (issue, affected_markets, severity 1-10)

JSON only, no markdown."""

            response = self.claude.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1500,
                messages=[{"role": "user", "content": prompt}]
            )
            
            text = response.content[0].text.strip()
            start = text.find('{')
            end = text.rfind('}') + 1
            if start >= 0 and end > start:
                predictions = json.loads(text[start:end])
                predictions['ai_generated'] = True
                predictions['confidence_overall'] = 0.75
                self.stats['ai_calls'] += 1
                print(f"  [AI] Generated predictions with Claude")
                return predictions
                
        except Exception as e:
            print(f"  [!] AI prediction failed: {e}")
        
        return self._generate_rule_predictions()
    
    def _generate_rule_predictions(self) -> Dict:
        """Generate predictions using rule-based analysis."""
        trending = self.market_pulse.get('trending_operators', [])
        markets = self.market_pulse.get('trending_markets', [])
        
        next_deals = []
        for i, op in enumerate(trending[:3]):
            next_deals.append({
                'buyer': op['name'] if i % 2 == 0 else 'Private Equity',
                'target': trending[i+1]['name'] if i+1 < len(trending) else 'Regional operator',
                'confidence': 0.4 - (i * 0.1),
                'rationale': f"High activity detected ({op['mentions']} mentions)"
            })
        
        capacity_forecast = {}
        for m in markets[:5]:
            capacity_forecast[m['name']] = {
                'expected_mw': m['mentions'] * 50,
                'growth_rate': '15-25%'
            }
        
        emerging = []
        for m in EMERGING_MARKETS[:3]:
            if m not in [x['name'] for x in markets]:
                emerging.append({
                    'name': m,
                    'why': 'Untapped market with infrastructure potential',
                    'confidence': 0.35
                })
        
        return {
            'next_deals': next_deals,
            'capacity_forecast': capacity_forecast,
            'emerging_hotspots': emerging,
            'risk_alerts': [
                {'issue': 'Grid capacity constraints', 'affected_markets': ['Northern Virginia', 'Phoenix'], 'severity': 7},
                {'issue': 'Permitting delays', 'affected_markets': ['California', 'New York'], 'severity': 5}
            ],
            'ai_generated': False,
            'confidence_overall': 0.45
        }
    
    def _store_predictions(self, predictions: Dict):
        """Store predictions in database for tracking accuracy."""
        import time as _time
        for attempt in range(5):
            try:
                conn = get_db(self.db_path)
                cursor = conn.cursor()
                
                for deal in predictions.get('next_deals', [])[:3]:
                    cursor.execute('''
                        INSERT INTO ai_predictions (prediction_type, prediction_data, confidence, timeframe)
                        VALUES (?, ?, ?, ?)
                    ''', ('deal', json.dumps(deal), deal.get('confidence', 0.5), '6_months'))
                
                for hotspot in predictions.get('emerging_hotspots', []):
                    cursor.execute('''
                        INSERT INTO ai_predictions (prediction_type, prediction_data, confidence, timeframe)
                        VALUES (?, ?, ?, ?)
                    ''', ('market_growth', json.dumps(hotspot), hotspot.get('confidence', 0.5), '12_months'))
                
                conn.commit()
                conn.close()
                return
            except sqlite3.OperationalError as e:
                if 'locked' in str(e) and attempt < 4:
                    try: conn.close()
                    except: pass
                    _time.sleep(3.0 * (attempt + 1))
                    continue
                print(f"  [!] Failed to store predictions: {e}")
                try: conn.close()
                except: pass
                return
            except Exception as e:
                print(f"  [!] Failed to store predictions: {e}")
                try: conn.close()
                except: pass
                return
    
    def hunt_opportunities(self) -> List[Dict]:
        """Proactively identify investment and deal opportunities."""
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] HUNTING OPPORTUNITIES")
        
        opportunities = []
        conn = get_db(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                SELECT title, summary, url, source FROM announcements
                WHERE published_date > datetime('now', '-3 days')
                ORDER BY published_date DESC LIMIT 100
            ''')
            recent_news = cursor.fetchall()
            
            for title, summary, link, source in recent_news:
                text = f"{title or ''} {summary or ''}".lower()
                opp = self._classify_opportunity(text, title, link, source)
                if opp:
                    opportunities.append(opp)
            
            cursor.execute('''
                SELECT operator, mw, location, notes FROM capacity_pipeline
                WHERE created_at > datetime('now', '-7 days')
                ORDER BY mw DESC LIMIT 20
            ''')
            new_capacity = cursor.fetchall()
            
            for operator, mw, location, notes in new_capacity:
                if mw and mw >= 100:
                    opportunities.append({
                        'type': 'large_capacity',
                        'title': f"{operator or 'Unknown'} adding {mw} MW in {location or 'Unknown'}",
                        'priority': 8 if mw >= 500 else 6,
                        'entities': [operator, location],
                        'rationale': 'Large capacity addition detected'
                    })
            
            cursor.execute('''
                SELECT buyer, seller, value, type, mw 
                FROM deals
                WHERE date > date('now', '-30 days')
                ORDER BY date DESC LIMIT 10
            ''')
            recent_deals = cursor.fetchall()
            
            for buyer, seller, target, deal_type, value in recent_deals:
                if value and value >= 100:
                    opportunities.append({
                        'type': 'major_deal',
                        'title': f"{buyer or 'Unknown'} {deal_type or 'deal'}: {target or 'Unknown'} (${value}M)",
                        'priority': 9 if value >= 1000 else 7,
                        'entities': [buyer, seller, target],
                        'rationale': 'Significant M&A activity'
                    })
            
        except Exception as e:
            print(f"  [!] Error hunting opportunities: {e}")
        finally:
            conn.close()
        
        opportunities.sort(key=lambda x: -x.get('priority', 0))
        top_opportunities = opportunities[:10]
        
        self._store_opportunities(top_opportunities)
        self.opportunity_queue = top_opportunities
        self.stats['opportunities_found'] += len(top_opportunities)
        
        print(f"  Found {len(top_opportunities)} high-priority opportunities")
        for opp in top_opportunities[:3]:
            print(f"    [{opp['priority']}] {opp['title'][:60]}")
        
        self._save_state()
        return top_opportunities
    
    def _classify_opportunity(self, text: str, title: str, link: str, source: str) -> Optional[Dict]:
        """Classify news as potential opportunity."""
        priority = 0
        opp_type = None
        rationale = []
        
        if any(kw in text for kw in ['acquire', 'acquisition', 'merger', 'buy out']):
            priority += 4
            opp_type = 'ma_activity'
            rationale.append('M&A keywords detected')
        
        if any(kw in text for kw in ['funding', 'raise', 'investment', 'series']):
            priority += 3
            opp_type = opp_type or 'funding'
            rationale.append('Funding activity')
        
        if any(kw in text for kw in ['expand', 'new campus', 'groundbreaking', 'construction']):
            priority += 2
            opp_type = opp_type or 'expansion'
            rationale.append('Expansion plans')
        
        mw_match = re.search(r'(\d{2,4})\s*MW', text, re.I)
        if mw_match:
            mw = int(mw_match.group(1))
            if mw >= 100:
                priority += 3
                rationale.append(f'{mw} MW capacity')
        
        value_match = re.search(r'\$(\d+(?:\.\d+)?)\s*(billion|million|B|M)', text, re.I)
        if value_match:
            value = float(value_match.group(1))
            unit = value_match.group(2).lower()
            if 'b' in unit:
                value *= 1000
            if value >= 100:
                priority += 4
                rationale.append(f'${value}M+ deal value')
        
        if priority >= 4:
            return {
                'type': opp_type or 'general',
                'title': title[:150] if title else 'Untitled',
                'link': link,
                'source': source,
                'priority': min(priority, 10),
                'rationale': '; '.join(rationale)
            }
        
        return None
    
    def _store_opportunities(self, opportunities: List[Dict]):
        """Store opportunities as alerts in database."""
        conn = get_db(self.db_path)
        cursor = conn.cursor()
        
        try:
            for opp in opportunities:
                cursor.execute('''
                    INSERT INTO opportunity_alerts 
                    (alert_type, title, description, entities, priority)
                    VALUES (?, ?, ?, ?, ?)
                ''', (
                    opp.get('type', 'general'),
                    opp.get('title', '')[:200],
                    opp.get('rationale', ''),
                    json.dumps(opp.get('entities', [])),
                    opp.get('priority', 5)
                ))
            conn.commit()
            self.stats['alerts_generated'] += len(opportunities)
        except Exception as e:
            print(f"  [!] Failed to store opportunities: {e}")
        finally:
            conn.close()
    
    def share_cross_agent_insights(self) -> Dict:
        """Share insights between all agents for coordinated intelligence."""
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] SHARING CROSS-AGENT INSIGHTS")
        
        insights = {
            'from_evolution': [],
            'from_deep_learning': [],
            'from_global_intel': [],
            'synthesized': []
        }
        
        conn = get_db(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                SELECT entity_type, entity_value, confidence, frequency
                FROM learned_entities
                WHERE confidence > 0.7
                ORDER BY frequency DESC LIMIT 20
            ''')
            for entity_type, value, conf, freq in cursor.fetchall():
                insights['from_deep_learning'].append({
                    'type': entity_type,
                    'value': value,
                    'confidence': conf,
                    'frequency': freq
                })
            
            cursor.execute('''
                SELECT action_type, description, details
                FROM evolution_log
                WHERE timestamp > datetime('now', '-7 days')
                ORDER BY timestamp DESC LIMIT 20
            ''')
            for action_type, desc, details in cursor.fetchall():
                insights['from_evolution'].append({
                    'action': action_type,
                    'description': desc
                })
            
            cursor.execute('''
                SELECT pattern_type, pattern_value, confidence
                FROM learning_patterns
                WHERE confidence > 0.6
                ORDER BY confidence DESC LIMIT 20
            ''')
            for ptype, pdata, conf in cursor.fetchall():
                try:
                    data = json.loads(pdata) if pdata else {}
                    insights['from_global_intel'].append({
                        'pattern': ptype,
                        'data': data,
                        'confidence': conf
                    })
                except:
                    pass
            
        except Exception as e:
            print(f"  [!] Error gathering insights: {e}")
        finally:
            conn.close()
        
        insights['synthesized'] = self._synthesize_insights(insights)
        
        self._broadcast_insights(insights['synthesized'])
        self.cross_agent_insights = insights
        self.stats['patterns_shared'] += len(insights['synthesized'])
        
        print(f"  Shared {len(insights['synthesized'])} synthesized insights")
        self._save_state()
        
        return insights
    
    def _synthesize_insights(self, raw_insights: Dict) -> List[Dict]:
        """Synthesize insights from multiple agents into actionable intelligence."""
        synthesized = []
        
        operators = {}
        for insight in raw_insights.get('from_deep_learning', []):
            if insight.get('type') == 'operator':
                operators[insight['value']] = {
                    'frequency': insight.get('frequency', 0),
                    'confidence': insight.get('confidence', 0.5)
                }
        
        for op, data in sorted(operators.items(), key=lambda x: -x[1]['frequency'])[:5]:
            synthesized.append({
                'insight_type': 'trending_operator',
                'entity': op,
                'signal_strength': data['confidence'],
                'recommendation': f'Monitor {op} for expansion/deal activity'
            })
        
        if self.market_pulse.get('sentiment') == 'bullish':
            synthesized.append({
                'insight_type': 'market_momentum',
                'signal': 'positive',
                'recommendation': 'Good time for capacity investments'
            })
        elif self.market_pulse.get('sentiment') == 'bearish':
            synthesized.append({
                'insight_type': 'market_momentum',
                'signal': 'caution',
                'recommendation': 'Monitor for acquisition opportunities at lower valuations'
            })
        
        return synthesized
    
    def _broadcast_insights(self, insights: List[Dict]):
        """Store insights for other agents to consume."""
        conn = get_db(self.db_path)
        cursor = conn.cursor()
        
        try:
            for insight in insights:
                cursor.execute('''
                    INSERT INTO agent_knowledge_share 
                    (from_agent, insight_type, insight_data, confidence)
                    VALUES (?, ?, ?, ?)
                ''', (
                    'orchestrator',
                    insight.get('insight_type', 'general'),
                    json.dumps(insight),
                    insight.get('signal_strength', 0.5)
                ))
            conn.commit()
        except Exception as e:
            print(f"  [!] Failed to broadcast insights: {e}")
        finally:
            conn.close()
    
    def detect_anomalies(self) -> List[Dict]:
        """Detect unusual patterns that warrant attention."""
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] ANOMALY DETECTION")
        
        anomalies = []
        conn = get_db(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                SELECT date(published_date) as day, COUNT(*) as count
                FROM announcements
                WHERE published_date > datetime('now', '-14 days')
                GROUP BY day
                ORDER BY day DESC
            ''')
            daily_news = cursor.fetchall()
            
            if len(daily_news) >= 3:
                counts = [x[1] for x in daily_news]
                avg = sum(counts) / len(counts)
                if counts[0] > avg * 2:
                    anomalies.append({
                        'type': 'news_spike',
                        'description': f"News volume {counts[0]} vs avg {avg:.0f}",
                        'severity': 'medium',
                        'action': 'Review recent news for major events'
                    })
            
            cursor.execute('''
                SELECT operator, SUM(mw) as total_mw
                FROM capacity_pipeline
                WHERE created_at > datetime('now', '-7 days')
                GROUP BY operator
                HAVING total_mw > 500
            ''')
            big_announcements = cursor.fetchall()
            
            for operator, total_mw in big_announcements:
                anomalies.append({
                    'type': 'large_capacity_announcement',
                    'description': f"{operator}: {total_mw} MW announced in 7 days",
                    'severity': 'high',
                    'action': f'Investigate {operator} expansion strategy'
                })
            
        except Exception as e:
            print(f"  [!] Error in anomaly detection: {e}")
        finally:
            conn.close()
        
        print(f"  Detected {len(anomalies)} anomalies")
        for a in anomalies:
            print(f"    [{a['severity'].upper()}] {a['description']}")
        
        return anomalies
    
    def run_orchestration_cycle(self) -> Dict:
        """Run a complete orchestration cycle coordinating all agents."""
        print(f"\n{'#'*60}")
        print(f"# AI ORCHESTRATOR - FULL CYCLE")
        print(f"# {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'#'*60}")
        
        start_time = time.time()
        
        pulse = self.analyze_market_pulse()
        predictions = self.generate_predictions()
        opportunities = self.hunt_opportunities()
        anomalies = self.detect_anomalies()
        insights = self.share_cross_agent_insights()
        
        duration = time.time() - start_time
        self.stats['orchestrations'] += 1
        self.stats['last_run'] = datetime.now().isoformat()
        
        result = {
            'success': True,
            'duration_seconds': round(duration, 2),
            'market_pulse': pulse,
            'predictions_count': len(predictions.get('next_deals', [])),
            'opportunities_count': len(opportunities),
            'anomalies_count': len(anomalies),
            'insights_shared': len(insights.get('synthesized', [])),
            'stats': self.stats,
            'timestamp': datetime.now().isoformat()
        }
        
        print(f"\n{'='*60}")
        print(f"ORCHESTRATION COMPLETE in {duration:.1f}s")
        print(f"  Market: {pulse.get('sentiment', 'unknown').upper()}")
        print(f"  Predictions: {result['predictions_count']}")
        print(f"  Opportunities: {result['opportunities_count']}")
        print(f"  Anomalies: {result['anomalies_count']}")
        print(f"{'='*60}\n")
        
        self._save_state()
        return result
    
    def get_proactive_recommendations(self) -> List[Dict]:
        """Get AI-generated proactive recommendations for the user."""
        recommendations = []
        
        if self.opportunity_queue:
            top = self.opportunity_queue[0]
            recommendations.append({
                'type': 'opportunity',
                'priority': 'high',
                'title': 'Hot Opportunity Detected',
                'description': top.get('title', ''),
                'action': 'Review and potentially act on this deal/expansion'
            })
        
        if self.predictions.get('emerging_hotspots'):
            hotspot = self.predictions['emerging_hotspots'][0]
            recommendations.append({
                'type': 'market',
                'priority': 'medium',
                'title': f"Emerging Market: {hotspot.get('name', 'Unknown')}",
                'description': hotspot.get('why', ''),
                'action': 'Consider early mover advantage in this market'
            })
        
        pulse = self.market_pulse
        if pulse.get('sentiment') == 'bullish' and pulse.get('sentiment_score', 0) > 0.7:
            recommendations.append({
                'type': 'market_timing',
                'priority': 'high',
                'title': 'Strong Bullish Market Sentiment',
                'description': f"Score: {pulse['sentiment_score']:.0%}, Pipeline: {pulse.get('capacity_pipeline_mw', 0):,.0f} MW",
                'action': 'Favorable conditions for capacity investments'
            })
        
        return recommendations
    
    def get_status(self) -> Dict:
        """Get current orchestrator status."""
        return {
            'status': 'active',
            'claude_enabled': self.claude is not None,
            'market_pulse': self.market_pulse,
            'predictions_count': len(self.predictions.get('next_deals', [])),
            'opportunities_queued': len(self.opportunity_queue),
            'stats': self.stats,
            'last_run': self.stats.get('last_run')
        }


orchestrator_instance = None

def get_orchestrator() -> AIOrchestrator:
    """Get or create the orchestrator singleton."""
    global orchestrator_instance
    if orchestrator_instance is None:
        orchestrator_instance = AIOrchestrator()
    return orchestrator_instance


def setup_orchestrator_routes(app):
    """Setup Flask routes for the orchestrator."""
    from flask import Blueprint, jsonify, request
    
    orchestrator_bp = Blueprint('orchestrator', __name__, url_prefix='/api/orchestrator')
    
    @orchestrator_bp.route('/status')
    def status():
        orch = get_orchestrator()
        return jsonify(orch.get_status())
    
    @orchestrator_bp.route('/run', methods=['POST'])
    def run_cycle():
        orch = get_orchestrator()
        result = orch.run_orchestration_cycle()
        return jsonify(result)
    
    @orchestrator_bp.route('/pulse')
    def market_pulse():
        orch = get_orchestrator()
        return jsonify(orch.market_pulse)
    
    @orchestrator_bp.route('/predictions')
    def predictions():
        orch = get_orchestrator()
        return jsonify(orch.predictions)
    
    @orchestrator_bp.route('/opportunities')
    def opportunities():
        orch = get_orchestrator()
        return jsonify({
            'success': True,
            'count': len(orch.opportunity_queue),
            'opportunities': orch.opportunity_queue
        })
    
    @orchestrator_bp.route('/recommendations')
    def recommendations():
        orch = get_orchestrator()
        recs = orch.get_proactive_recommendations()
        return jsonify({
            'success': True,
            'count': len(recs),
            'recommendations': recs
        })
    
    @orchestrator_bp.route('/insights')
    def insights():
        orch = get_orchestrator()
        return jsonify({
            'success': True,
            'insights': orch.cross_agent_insights
        })
    
    @orchestrator_bp.route('/anomalies', methods=['POST'])
    def detect_anomalies():
        orch = get_orchestrator()
        anomalies = orch.detect_anomalies()
        return jsonify({
            'success': True,
            'count': len(anomalies),
            'anomalies': anomalies
        })
    
    app.register_blueprint(orchestrator_bp)
    print("  Orchestrator API: /api/orchestrator/*")
    
    return app


if __name__ == '__main__':
    orch = AIOrchestrator()
    result = orch.run_orchestration_cycle()
    print(json.dumps(result, indent=2))
