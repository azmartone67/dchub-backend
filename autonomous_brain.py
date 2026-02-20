"""
Autonomous Brain - Master AI Agent for DC Hub
Self-learning, self-improving, fully autonomous system
Runs continuously without manual intervention
"""

import sqlite3
import json
import re
import os
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional

logger = logging.getLogger(__name__)

class AutonomousBrain:
    """Master autonomous agent that coordinates all learning systems"""
    
    def __init__(self, db_path: str = 'dc_nexus.db'):
        self.db_path = db_path
        self.state_file = 'data/autonomous_brain_state.json'
        self.running = False
        self.scheduler_thread = None
        self.state = self._load_state()
        
        self.mw_patterns = [
            r'(\d+(?:,\d+)?(?:\.\d+)?)\s*(MW|megawatt)',
            r'(\d+(?:,\d+)?(?:\.\d+)?)\s*(GW|gigawatt)',
            r'capacity\s+of\s+(\d+(?:,\d+)?(?:\.\d+)?)\s*(MW|GW)',
            r'(\d+(?:,\d+)?(?:\.\d+)?)\s*(MW|GW)\s+(?:facility|campus|data center)',
            r'(\d+(?:,\d+)?(?:\.\d+)?)\s*(MW|GW)\s+expansion',
            r'(\d+(?:,\d+)?(?:\.\d+)?)\s*(MW|GW)\s+project',
            r'(\d+(?:,\d+)?(?:\.\d+)?)\s*(MW|GW)\s+capacity',
            r'power\s+capacity.*?(\d+(?:,\d+)?(?:\.\d+)?)\s*(MW|GW)',
            r'(\d+(?:,\d+)?(?:\.\d+)?)\s*(MW|GW)\s+of\s+(?:IT\s+)?(?:power|load)',
        ]
        
        self.deal_patterns = [
            r'acquir(?:e[sd]?|ing|ition)',
            r'merg(?:e[sd]?|ing|er)',
            r'buy(?:s|ing)?|bought|purchase[sd]?',
            r'sell(?:s|ing)?|sold|divest',
            r'\$\s*(\d+(?:\.\d+)?)\s*(?:billion|million|B|M)',
            r'joint\s+venture',
            r'partnership',
            r'investment',
        ]
        
        self.api_sources = [
            {'name': 'PeeringDB', 'url': 'https://www.peeringdb.com/api/', 'type': 'facilities'},
            {'name': 'OpenStreetMap', 'url': 'https://overpass-api.de/api/', 'type': 'facilities'},
            {'name': 'Wikidata', 'url': 'https://query.wikidata.org/sparql', 'type': 'entities'},
            {'name': 'SEC EDGAR', 'url': 'https://data.sec.gov/', 'type': 'filings'},
            {'name': 'FCC', 'url': 'https://opendata.fcc.gov/', 'type': 'infrastructure'},
            {'name': 'HIFLD', 'url': 'https://hifld-geoplatform.opendata.arcgis.com/', 'type': 'infrastructure'},
        ]
        
    def _load_state(self) -> Dict:
        """Load state from file"""
        try:
            os.makedirs('data', exist_ok=True)
            if os.path.exists(self.state_file):
                with open(self.state_file, 'r') as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"Error loading state: {e}")
        
        return {
            'total_cycles': 0,
            'capacity_extracted': 0,
            'deals_found': 0,
            'quality_fixes': 0,
            'apis_discovered': 0,
            'last_cycle': None,
            'insights': [],
            'autonomous_actions': [],
            'learning_rate': 1.0,
        }
    
    def _save_state(self):
        """Save state to file"""
        try:
            os.makedirs('data', exist_ok=True)
            with open(self.state_file, 'w') as f:
                json.dump(self.state, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Error saving state: {e}")
    
    def _get_db(self):
        """Get database connection with WAL mode and timeout"""
        conn = get_db(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    
    def extract_capacity_from_news(self) -> Dict:
        """Enhanced MW/GW extraction from news articles"""
        results = {'extracted': 0, 'new_pipeline': 0, 'total_mw': 0}
        
        try:
            conn = self._get_db()
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT id, title, content, source, source_url as link, published_date
                FROM announcements 
                WHERE (title LIKE '%MW%' OR title LIKE '%GW%' 
                    OR title LIKE '%megawatt%' OR title LIKE '%gigawatt%'
                    OR title LIKE '%capacity%' OR title LIKE '%power%'
                    OR content LIKE '%MW%' OR content LIKE '%GW%')
                ORDER BY published_date DESC
                LIMIT 500
            """)
            
            articles = cursor.fetchall()
            
            for article in articles:
                text = f"{article['title']} {article['content'] or ''}"
                
                for pattern in self.mw_patterns:
                    matches = re.findall(pattern, text, re.IGNORECASE)
                    for match in matches:
                        try:
                            if isinstance(match, tuple) and len(match) >= 2:
                                value_str, unit = match[0], match[1]
                            else:
                                continue
                            
                            value = float(value_str.replace(',', ''))
                            
                            if unit.upper() in ['GW', 'GIGAWATT']:
                                value *= 1000
                            
                            if 1 <= value <= 10000:
                                results['extracted'] += 1
                                results['total_mw'] += value
                                
                                operator = self._extract_operator(text)
                                location = self._extract_location(text)
                                
                                cursor.execute("""
                                    SELECT id FROM capacity_pipeline 
                                    WHERE source_url = ? OR (operator = ? AND capacity_mw = ?)
                                """, (article['link'], operator, value))
                                
                                if not cursor.fetchone():
                                    cursor.execute("""
                                        INSERT OR IGNORE INTO capacity_pipeline 
                                        (operator, capacity_mw, market, status, source_url, source, created_at)
                                        VALUES (?, ?, ?, 'announced', ?, 'auto_extracted', datetime('now'))
                                    """, (operator, value, location, article['link']))
                                    results['new_pipeline'] += 1
                                    
                        except (ValueError, TypeError):
                            continue
            
            conn.commit()
            conn.close()
            
            self.state['capacity_extracted'] += results['new_pipeline']
            self._save_state()
            
        except Exception as e:
            logger.error(f"Capacity extraction error: {e}")
        
        return results
    
    def extract_deals_from_news(self) -> Dict:
        """Extract M&A deals from news articles"""
        results = {'scanned': 0, 'deals_found': 0}
        
        try:
            conn = self._get_db()
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT id, title, content, source, source_url as link, published_date
                FROM announcements 
                WHERE (title LIKE '%acqui%' OR title LIKE '%merg%' 
                    OR title LIKE '%buy%' OR title LIKE '%sell%'
                    OR title LIKE '%deal%' OR title LIKE '%billion%'
                    OR title LIKE '%million%')
                ORDER BY published_date DESC
                LIMIT 300
            """)
            
            articles = cursor.fetchall()
            
            for article in articles:
                results['scanned'] += 1
                text = f"{article['title']} {article['content'] or ''}"
                
                deal_score = 0
                for pattern in self.deal_patterns:
                    if re.search(pattern, text, re.IGNORECASE):
                        deal_score += 1
                
                if deal_score >= 2:
                    value_match = re.search(r'\$\s*(\d+(?:\.\d+)?)\s*(billion|million|B|M)', text, re.IGNORECASE)
                    deal_value = None
                    if value_match:
                        val = float(value_match.group(1))
                        unit = value_match.group(2).lower()
                        if unit in ['billion', 'b']:
                            deal_value = val * 1000000000
                        else:
                            deal_value = val * 1000000
                    
                    buyer, target = self._extract_deal_parties(text)
                    
                    cursor.execute("""
                        SELECT id FROM deals WHERE source_url = ?
                    """, (article['link'],))
                    
                    if not cursor.fetchone() and (buyer or target):
                        import uuid
                        cursor.execute("""
                            INSERT OR IGNORE INTO deals 
                            (id, buyer, seller, value, type, status, 
                             source_url, notes, created_at)
                            VALUES (?, ?, ?, ?, 'acquisition', 'announced', 
                                    ?, ?, datetime('now'))
                        """, (str(uuid.uuid4())[:8], buyer, target, deal_value, article['link'], 
                              article['title']))
                        results['deals_found'] += 1
            
            conn.commit()
            conn.close()
            
            self.state['deals_found'] += results['deals_found']
            self._save_state()
            
        except Exception as e:
            logger.error(f"Deal extraction error: {e}")
        
        return results
    
    def auto_fix_quality_issues(self) -> Dict:
        """Automatically fix common quality issues"""
        results = {'checked': 0, 'fixed': 0, 'categories': {}}
        
        import time as _time
        for attempt in range(5):
            try:
                conn = self._get_db()
                cursor = conn.cursor()
                
                cursor.execute("""
                    UPDATE facilities SET country = 'US' 
                    WHERE country IS NULL AND (
                        city LIKE '%Virginia%' OR city LIKE '%Texas%' 
                        OR city LIKE '%California%' OR city LIKE '%Arizona%'
                        OR state IN ('VA', 'TX', 'CA', 'AZ', 'NY', 'NJ', 'IL', 'GA', 'NC', 'OH')
                    )
                """)
                results['categories']['country_fixed'] = cursor.rowcount
                results['fixed'] += cursor.rowcount
                
                cursor.execute("""
                    UPDATE facilities SET status = 'active' 
                    WHERE status IS NULL OR status = ''
                """)
                results['categories']['status_fixed'] = cursor.rowcount
                results['fixed'] += cursor.rowcount
                
                cursor.execute("""
                    UPDATE facilities SET provider = name 
                    WHERE provider IS NULL AND name IS NOT NULL
                """)
                results['categories']['provider_fixed'] = cursor.rowcount
                results['fixed'] += cursor.rowcount
                
                cursor.execute("""
                    DELETE FROM announcements 
                    WHERE title IS NULL OR title = '' OR length(title) < 10
                """)
                results['categories']['empty_news_removed'] = cursor.rowcount
                
                cursor.execute("""
                    UPDATE facilities 
                    SET name = TRIM(name),
                        city = TRIM(city),
                        provider = TRIM(provider)
                    WHERE name LIKE ' %' OR name LIKE '% '
                       OR city LIKE ' %' OR city LIKE '% '
                       OR provider LIKE ' %' OR provider LIKE '% '
                """)
                results['categories']['whitespace_trimmed'] = cursor.rowcount
                results['fixed'] += cursor.rowcount
                
                conn.commit()
                conn.close()
                
                self.state['quality_fixes'] += results['fixed']
                self._save_state()
                break
                
            except sqlite3.OperationalError as e:
                if 'locked' in str(e) and attempt < 4:
                    _time.sleep(5.0 * (attempt + 1))
                    continue
                logger.error(f"Quality fix error: {e}")
            except Exception as e:
                logger.error(f"Quality fix error: {e}")
                break
        
        return results
    
    def discover_new_apis(self) -> Dict:
        """Discover and test new data APIs"""
        results = {'checked': 0, 'new_found': 0, 'working': [], 'failed': []}
        
        import requests
        
        for source in self.api_sources:
            results['checked'] += 1
            try:
                resp = requests.get(source['url'], timeout=10)
                if resp.status_code == 200:
                    results['working'].append(source['name'])
                    logger.debug(f"   🔌 API check passed: {source['name']}")
                else:
                    results['failed'].append({'name': source['name'], 'status': resp.status_code})
                    logger.debug(f"   ⚠️ API check failed: {source['name']} ({resp.status_code})")
            except requests.RequestException as e:
                results['failed'].append({'name': source['name'], 'error': str(e)})
                logger.debug(f"   ❌ API check error: {source['name']} - {e}")
            except Exception as e:
                results['failed'].append({'name': source['name'], 'error': str(e)})
                logger.warning(f"   ❌ Unexpected API check error: {source['name']} - {e}")
        
        self.state['apis_discovered'] = len(results['working'])
        self._save_state()
        
        return results
    
    def sync_infrastructure(self) -> Dict:
        """Sync land and power infrastructure data"""
        results = {'fiber': 0, 'substations': 0, 'permits': 0, 'properties': 0}
        
        try:
            from infrastructure_discovery import (
                FiberRouteDiscovery, SubstationDiscovery, 
                ConstructionPermitDiscovery, DCPropertyDiscovery
            )
            
            fiber = FiberRouteDiscovery()
            results['fiber'] = fiber.sync()
            
            substations = SubstationDiscovery()
            results['substations'] = substations.sync()
            
            permits = ConstructionPermitDiscovery()
            results['permits'] = permits.sync()
            
            properties = DCPropertyDiscovery()
            results['properties'] = properties.sync()
            
            self.state['infrastructure_syncs'] = self.state.get('infrastructure_syncs', 0) + 1
            self._save_state()
            
        except Exception as e:
            logger.error(f"Infrastructure sync error: {e}")
        
        return results
    
    def extract_infrastructure_from_news(self) -> Dict:
        """Extract infrastructure data from news articles"""
        results = {'fiber_mentions': 0, 'power_mentions': 0, 'land_mentions': 0, 'added': 0}
        
        try:
            conn = self._get_db()
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT id, title, content, source_url FROM announcements
                WHERE published_date > datetime('now', '-3 days')
                ORDER BY published_date DESC LIMIT 200
            ''')
            articles = [{'id': r[0], 'title': r[1], 'content': r[2], 'url': r[3]} for r in cursor.fetchall()]
            
            fiber_patterns = [
                r'(\d+(?:,\d+)?)\s*(?:mile|km)\s*(?:fiber|route)',
                r'fiber\s+(?:route|network|cable)',
                r'submarine\s+cable',
                r'dark\s+fiber',
            ]
            
            power_patterns = [
                r'(\d+(?:,\d+)?)\s*(?:MW|GW)\s*(?:substation|transformer)',
                r'power\s+substation',
                r'electrical\s+infrastructure',
                r'utility\s+(?:agreement|contract)',
            ]
            
            land_patterns = [
                r'(\d+(?:,\d+)?)\s*(?:acre|hectare)',
                r'land\s+(?:acquisition|purchase|deal)',
                r'site\s+(?:selection|development)',
                r'construction\s+permit',
            ]
            
            for article in articles:
                text = f"{article['title']} {article['content'] or ''}"
                
                for pattern in fiber_patterns:
                    if re.search(pattern, text, re.IGNORECASE):
                        results['fiber_mentions'] += 1
                        break
                
                for pattern in power_patterns:
                    if re.search(pattern, text, re.IGNORECASE):
                        results['power_mentions'] += 1
                        break
                
                for pattern in land_patterns:
                    if re.search(pattern, text, re.IGNORECASE):
                        results['land_mentions'] += 1
                        self._extract_and_save_permit(article, text, cursor)
                        break
            
            conn.commit()
            conn.close()
            
        except Exception as e:
            logger.error(f"Infrastructure news extraction error: {e}")
        
        return results
    
    def _extract_and_save_permit(self, article: Dict, text: str, cursor):
        """Extract and save construction permit from article"""
        try:
            acre_match = re.search(r'(\d+(?:,\d+)?)\s*(?:acre|hectare)', text, re.IGNORECASE)
            mw_match = re.search(r'(\d+(?:,\d+)?(?:\.\d+)?)\s*(MW|GW)', text, re.IGNORECASE)
            
            location = self._extract_location(text)
            operator = self._extract_operator(text)
            
            if acre_match or mw_match:
                source_id = f"news_{article['id']}"
                estimated_mw = None
                
                if mw_match:
                    value = float(mw_match.group(1).replace(',', ''))
                    if mw_match.group(2).upper() in ['GW', 'GIGAWATT']:
                        value *= 1000
                    estimated_mw = value
                
                cursor.execute('''
                    INSERT OR IGNORE INTO construction_permits
                    (project_name, city, state, owner, estimated_power_mw, source, source_id, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    article['title'][:200],
                    location if location != 'Unknown' else None,
                    None,
                    operator if operator != 'Unknown' else None,
                    estimated_mw,
                    'news_extraction',
                    source_id,
                    'announced'
                ))
        except Exception as e:
            logger.debug(f"Permit extraction error: {e}")
    
    def run_autonomous_cycle(self) -> Dict:
        """Run a complete autonomous learning cycle"""
        start_time = datetime.now()
        
        results = {
            'cycle_id': self.state['total_cycles'] + 1,
            'started_at': start_time.isoformat(),
            'capacity': {},
            'deals': {},
            'quality': {},
            'infrastructure': {},
            'apis': {},
        }
        
        logger.info(f"🧠 Autonomous Brain - Cycle {results['cycle_id']} starting...")
        
        results['capacity'] = self.extract_capacity_from_news()
        logger.info(f"   📊 Capacity: {results['capacity'].get('new_pipeline', 0)} new entries")
        
        results['deals'] = self.extract_deals_from_news()
        logger.info(f"   💰 Deals: {results['deals'].get('deals_found', 0)} found")
        
        results['quality'] = self.auto_fix_quality_issues()
        logger.info(f"   🔧 Quality: {results['quality'].get('fixed', 0)} fixes")
        
        results['infrastructure'] = self.extract_infrastructure_from_news()
        logger.info(f"   🔌 Infrastructure: {results['infrastructure'].get('fiber_mentions', 0)} fiber, {results['infrastructure'].get('power_mentions', 0)} power mentions")
        
        if self.state['total_cycles'] % 10 == 0:
            infra_sync = self.sync_infrastructure()
            results['infrastructure']['sync'] = infra_sync
            logger.info(f"   🏗️ Infra Sync: {infra_sync.get('fiber', 0)} fiber, {infra_sync.get('substations', 0)} substations")
        
        if self.state['total_cycles'] % 10 == 0:
            results['apis'] = self.discover_new_apis()
            logger.info(f"   🔌 APIs: {len(results['apis'].get('working', []))} working")
        
        self.state['total_cycles'] += 1
        self.state['last_cycle'] = datetime.now().isoformat()
        
        action = {
            'timestamp': datetime.now().isoformat(),
            'cycle': results['cycle_id'],
            'capacity_added': results['capacity'].get('new_pipeline', 0),
            'deals_found': results['deals'].get('deals_found', 0),
            'quality_fixes': results['quality'].get('fixed', 0),
        }
        self.state['autonomous_actions'] = self.state.get('autonomous_actions', [])[-99:] + [action]
        
        self._save_state()
        
        duration = (datetime.now() - start_time).total_seconds()
        results['duration_seconds'] = duration
        logger.info(f"🧠 Autonomous Brain - Cycle {results['cycle_id']} complete in {duration:.1f}s")
        
        return results
    
    def _extract_operator(self, text: str) -> str:
        """Extract operator name from text"""
        known_operators = [
            'Equinix', 'Digital Realty', 'CyrusOne', 'QTS', 'CoreSite',
            'Vantage', 'DataBank', 'Flexential', 'Switch', 'NTT',
            'Microsoft', 'Amazon', 'Google', 'Meta', 'Apple',
            'AWS', 'Azure', 'GCP', 'Oracle', 'IBM',
            'Stack Infrastructure', 'Prime Data Centers', 'EdgeCore',
            'Applied Digital', 'Compass', 'Stream Data Centers',
        ]
        
        for op in known_operators:
            if op.lower() in text.lower():
                return op
        
        return 'Unknown'
    
    def _extract_location(self, text: str) -> str:
        """Extract location from text"""
        locations = [
            'Ashburn', 'Virginia', 'Dallas', 'Texas', 'Phoenix', 'Arizona',
            'Silicon Valley', 'California', 'Chicago', 'Illinois',
            'New York', 'New Jersey', 'Atlanta', 'Georgia', 'Denver', 'Colorado',
            'Singapore', 'London', 'Frankfurt', 'Amsterdam', 'Tokyo', 'Sydney',
        ]
        
        for loc in locations:
            if loc.lower() in text.lower():
                return loc
        
        return 'Unknown'
    
    def _extract_deal_parties(self, text: str) -> tuple:
        """Extract buyer and target from deal text"""
        buyer = None
        target = None
        
        acquire_match = re.search(r'(\w+(?:\s+\w+)?)\s+(?:to\s+)?acquir(?:e[sd]?|ing)\s+(\w+(?:\s+\w+)?)', text, re.IGNORECASE)
        if acquire_match:
            buyer = acquire_match.group(1)
            target = acquire_match.group(2)
        
        buy_match = re.search(r'(\w+(?:\s+\w+)?)\s+(?:to\s+)?buy(?:s|ing)?\s+(\w+(?:\s+\w+)?)', text, re.IGNORECASE)
        if buy_match and not buyer:
            buyer = buy_match.group(1)
            target = buy_match.group(2)
        
        return (buyer, target)
    
    def get_status(self) -> Dict:
        """Get current brain status"""
        return {
            'active': self.running,
            'total_cycles': self.state.get('total_cycles', 0),
            'capacity_extracted': self.state.get('capacity_extracted', 0),
            'deals_found': self.state.get('deals_found', 0),
            'quality_fixes': self.state.get('quality_fixes', 0),
            'apis_discovered': self.state.get('apis_discovered', 0),
            'last_cycle': self.state.get('last_cycle'),
            'recent_actions': self.state.get('autonomous_actions', [])[-10:],
        }
    
    def start_scheduler(self, interval_seconds: int = 300):
        """Start the autonomous scheduler"""
        if self.running:
            logger.info("🧠 Autonomous Brain already running")
            return
        
        self.running = True
        self._stop_event = threading.Event()
        
        def run_loop():
            logger.info(f"🧠 Autonomous Brain loop started")
            while self.running and not self._stop_event.is_set():
                try:
                    self.run_autonomous_cycle()
                except sqlite3.Error as e:
                    logger.error(f"🧠 Autonomous cycle database error: {e}")
                except Exception as e:
                    logger.error(f"🧠 Autonomous cycle error: {e}", exc_info=True)
                
                self._stop_event.wait(interval_seconds)
            logger.info("🧠 Autonomous Brain loop stopped")
        
        self.scheduler_thread = threading.Thread(target=run_loop, daemon=True, name="AutonomousBrain")
        self.scheduler_thread.start()
        logger.info(f"🧠 Autonomous Brain started - running every {interval_seconds}s")
    
    def stop_scheduler(self):
        """Stop the autonomous scheduler"""
        self.running = False
        if hasattr(self, '_stop_event'):
            self._stop_event.set()
        logger.info("🧠 Autonomous Brain stopped")


from flask import Blueprint, jsonify, request
from db_utils import get_db

autonomous_bp = Blueprint('autonomous', __name__)
brain = AutonomousBrain()

@autonomous_bp.route('/api/autonomous/status', methods=['GET', 'OPTIONS'])
def get_brain_status():
    """Get autonomous brain status"""
    if request.method == 'OPTIONS':
        return '', 204
    return jsonify({
        'success': True,
        **brain.get_status()
    })

@autonomous_bp.route('/api/autonomous/run', methods=['POST', 'OPTIONS'])
def run_cycle():
    """Manually trigger an autonomous cycle"""
    if request.method == 'OPTIONS':
        return '', 204
    results = brain.run_autonomous_cycle()
    return jsonify({
        'success': True,
        'results': results
    })

@autonomous_bp.route('/api/autonomous/start', methods=['POST', 'OPTIONS'])
def start_brain():
    """Start the autonomous scheduler"""
    if request.method == 'OPTIONS':
        return '', 204
    interval = request.args.get('interval', 300, type=int)
    brain.start_scheduler(interval)
    return jsonify({
        'success': True,
        'message': f'Autonomous brain started with {interval}s interval'
    })

@autonomous_bp.route('/api/autonomous/stop', methods=['POST', 'OPTIONS'])
def stop_brain():
    """Stop the autonomous scheduler"""
    if request.method == 'OPTIONS':
        return '', 204
    brain.stop_scheduler()
    return jsonify({
        'success': True,
        'message': 'Autonomous brain stopped'
    })


def init_autonomous_brain():
    """Initialize and start the autonomous brain"""
    brain.start_scheduler(interval_seconds=300)
    return brain
