"""
Autonomous Brain - Master AI Agent for DC Hub
Self-learning, self-improving, fully autonomous system
Runs continuously without manual intervention
"""

import json
import re
import hashlib
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
import psycopg2
from psycopg2.extras import RealDictCursor

from db_utils import get_db

logger = logging.getLogger(__name__)


class AutonomousBrain:
    """Master autonomous agent that coordinates all learning systems"""

    def __init__(self):
        """Initialize Autonomous Brain with PostgreSQL backend"""
        self.running = False
        self.scheduler_thread = None
        self.state = self._load_state()

        # Initialize state in database on first run
        self._init_db_state()

        # Power capacity extraction patterns (MW/GW)
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

        # Deal and M&A patterns
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

        # Gas infrastructure patterns (pipelines, midstream, LNG)
        self.gas_patterns = [
            r'(\d+(?:,\d+)?)\s*(?:mile|km)\s*(?:gas\s+)?pipeline',
            r'(?:interstate|intrastate)\s+gas\s+pipeline',
            r'midstream\s+(?:gas|infrastructure)',
            r'NGL\s+(?:pipeline|facility|terminal)',
            r'LNG\s+(?:terminal|export|import|facility)',
            r'gas\s+(?:transmission|distribution|gathering)',
            r'natural\s+gas\s+(?:infrastructure|network)',
        ]

        # Transmission line patterns (power grid infrastructure)
        self.transmission_patterns = [
            r'(\d+)\s*kV\s+(?:transmission|line|substation)',
            r'HVDC\s+(?:line|transmission|corridor)',
            r'(?:765|500|345|230|138|69)\s*kV',
            r'transmission\s+(?:corridor|route|line)',
            r'substation\s+(?:interconnect|capacity|voltage)',
            r'electrical\s+transmission\s+(?:grid|network)',
        ]

        # Dark fiber and lit fiber patterns
        self.fiber_patterns = [
            r'(\d+(?:,\d+)?)\s*(?:mile|km)\s*(?:fiber|route)',
            r'(?:dark|lit)\s+fiber\s+(?:route|network|cable)',
            r'(?:Zayo|Lumen|Crown\s+Castle|Windstream|Level3|Cogent)\s+(?:fiber|network)',
            r'fiber\s+(?:route|network|cable|infrastructure)',
            r'submarine\s+cable',
            r'dark\s+fiber\s+(?:route|available)',
            r'fiber\s+optic\s+(?:network|infrastructure)',
        ]

        # Substation patterns
        self.substation_patterns = [
            r'(\d+)\s*kV\s+substation',
            r'transformer\s+(?:capacity|rating)\s*(?:MVA|kVA)',
            r'substation\s+(?:voltage|capacity|interconnect)',
            r'electrical\s+substation\s+(?:upgrade|expansion)',
        ]

        # Power plant patterns
        self.power_plant_patterns = [
            r'(?:natural\s+)?gas\s+(?:power|generation|plant)',
            r'nuclear\s+(?:power|plant|facility)',
            r'solar\s+(?:farm|facility|installation)',
            r'wind\s+(?:farm|turbine|facility)',
            r'power\s+(?:generation|plant)\s+(?:facility|site)',
        ]

        # API sources for discovery
        self.api_sources = [
            {'name': 'PeeringDB', 'url': 'https://www.peeringdb.com/api/', 'type': 'facilities'},
            {'name': 'OpenStreetMap', 'url': 'https://overpass-api.de/api/', 'type': 'facilities'},
            {'name': 'Wikidata', 'url': 'https://query.wikidata.org/sparql', 'type': 'entities'},
            {'name': 'SEC EDGAR', 'url': 'https://data.sec.gov/', 'type': 'filings'},
            {'name': 'FCC', 'url': 'https://opendata.fcc.gov/', 'type': 'infrastructure'},
            {'name': 'HIFLD', 'url': 'https://hifld-geoplatform.opendata.arcgis.com/', 'type': 'infrastructure'},
        ]

    def _init_db_state(self):
        """Initialize brain_state table in PostgreSQL if it doesn't exist"""
        conn = None
        try:
            conn = get_db()
            try:
                cur = conn.cursor()

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS brain_state (
                        id SERIAL PRIMARY KEY,
                        state_key VARCHAR(100) UNIQUE NOT NULL,
                        state_value JSONB,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                conn.commit()
                cur.close()
            finally:
                conn.close()
            conn = None
            logger.info("brain_state table initialized in PostgreSQL")
        except Exception as e:
            logger.error(f"Error initializing brain_state table: {e}")
            if conn:
                try:
                    conn.close()
                except:
                    pass

    def _load_state(self) -> Dict:
        """Load state from PostgreSQL brain_state table"""
        conn = None
        try:
            conn = get_db()
            try:
                cur = conn.cursor()

                cur.execute("""
                    SELECT state_value FROM brain_state
                    WHERE state_key = 'autonomous_brain_state'
                """)

                result = cur.fetchone()
                cur.close()
            finally:
                conn.close()
            conn = None

            if result:
                val = result['state_value'] if isinstance(result, dict) else result[0]
                if val:
                    logger.debug("Loaded state from PostgreSQL")
                    return val
        except Exception as e:
            logger.debug(f"Error loading state from PostgreSQL: {e}")
            if conn:
                try:
                    conn.close()
                except:
                    pass

        # Default state if not found
        return {
            'total_cycles': 0,
            'capacity_extracted': 0,
            'deals_found': 0,
            'quality_fixes': 0,
            'apis_discovered': 0,
            'gas_infrastructure_found': 0,
            'transmission_infrastructure_found': 0,
            'fiber_infrastructure_found': 0,
            'substation_infrastructure_found': 0,
            'power_plant_infrastructure_found': 0,
            'last_cycle': None,
            'insights': [],
            'autonomous_actions': [],
            'learning_rate': 1.0,
        }

    def _save_state(self):
        """Save state to PostgreSQL brain_state table"""
        try:
            conn = get_db()
            try:
                cur = conn.cursor()

                state_json = json.dumps(self.state, default=str)

                cur.execute("""
                    INSERT INTO brain_state (state_key, state_value, updated_at)
                    VALUES (%s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (state_key)
                    DO UPDATE SET
                        state_value = %s,
                        updated_at = CURRENT_TIMESTAMP
                """, ('autonomous_brain_state', state_json, state_json))

                conn.commit()
                cur.close()
            finally:
                conn.close()
            logger.debug("State saved to PostgreSQL")
        except Exception as e:
            logger.error(f"Error saving state to PostgreSQL: {e}")

    def extract_capacity_from_news(self) -> Dict:
        """Enhanced MW/GW extraction from news articles"""
        results = {'extracted': 0, 'new_pipeline': 0, 'total_mw': 0}

        try:
            conn = get_db()
            try:
                cur = conn.cursor(cursor_factory=RealDictCursor)

                cur.execute("""
                    SELECT id, title, COALESCE(content, summary, '') AS content, source, source_url, published_date
                    FROM announcements
                    WHERE (title ILIKE '%MW%' OR title ILIKE '%GW%'
                        OR title ILIKE '%megawatt%' OR title ILIKE '%gigawatt%'
                        OR title ILIKE '%capacity%' OR title ILIKE '%power%'
                        OR content ILIKE '%MW%' OR content ILIKE '%GW%')
                    ORDER BY published_date DESC
                    LIMIT 500
                """)

                articles = cur.fetchall()

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

                                    cur.execute("""
                                        SELECT id FROM capacity_pipeline
                                        WHERE source_url = %s OR (operator = %s AND capacity_mw = %s)
                                    """, (article['source_url'], operator, value))

                                    if not cur.fetchone():
                                        cur.execute("""
                                            INSERT INTO capacity_pipeline
                                            (operator, capacity_mw, market, status, source_url, source, created_at)
                                            VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                                            ON CONFLICT DO NOTHING
                                        """, (operator, value, location, 'announced', article['source_url'], 'auto_extracted'))
                                        results['new_pipeline'] += 1

                            except (ValueError, TypeError):
                                continue

                conn.commit()
                cur.close()
            finally:
                conn.close()

            self.state['capacity_extracted'] += results['new_pipeline']
            self._save_state()

        except Exception as e:
            logger.error(f"Capacity extraction error: {e}")
            results['error'] = str(e)

        return results

    def extract_deals_from_news(self) -> Dict:
        """Extract M&A deals from news articles"""
        results = {'scanned': 0, 'deals_found': 0}

        try:
            conn = get_db()
            try:
                cur = conn.cursor(cursor_factory=RealDictCursor)

                cur.execute("""
                    SELECT id, title, COALESCE(content, summary, '') AS content, source, source_url, published_date
                    FROM announcements
                    WHERE (title ILIKE '%acqui%' OR title ILIKE '%merg%'
                        OR title ILIKE '%buy%' OR title ILIKE '%sell%'
                        OR title ILIKE '%deal%' OR title ILIKE '%billion%'
                        OR title ILIKE '%million%')
                    ORDER BY published_date DESC
                    LIMIT 300
                """)

                articles = cur.fetchall()

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

                        cur.execute("""
                            SELECT id FROM deals WHERE source_url = %s
                        """, (article['source_url'],))

                        if not cur.fetchone() and (buyer or target):
                            import uuid
                            cur.execute("""
                                INSERT INTO deals
                                (id, buyer, seller, value, type, status,
                                 source_url, notes, created_at)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                                ON CONFLICT DO NOTHING
                            """, (str(uuid.uuid4())[:8], buyer, target, deal_value, 'acquisition', 'announced',
                                  article['source_url'], article['title'][:300]))
                            results['deals_found'] += 1

                conn.commit()
                cur.close()
            finally:
                conn.close()

            self.state['deals_found'] += results['deals_found']
            self._save_state()

        except Exception as e:
            logger.error(f"Deal extraction error: {e}")
            results['error'] = str(e)

        return results

    def auto_fix_quality_issues(self) -> Dict:
        """Automatically fix common quality issues"""
        results = {'checked': 0, 'fixed': 0, 'categories': {}}

        try:
            conn = get_db()
            try:
                cur = conn.cursor()

                # Fix missing country
                cur.execute("""
                    UPDATE facilities SET country = 'US'
                    WHERE country IS NULL AND (
                        city ILIKE '%Virginia%' OR city ILIKE '%Texas%'
                        OR city ILIKE '%California%' OR city ILIKE '%Arizona%'
                        OR state IN ('VA', 'TX', 'CA', 'AZ', 'NY', 'NJ', 'IL', 'GA', 'NC', 'OH')
                    )
                """)
                results['categories']['country_fixed'] = cur.rowcount
                results['fixed'] += cur.rowcount

                # Fix missing status
                cur.execute("""
                    UPDATE facilities SET status = 'active'
                    WHERE status IS NULL OR status = ''
                """)
                results['categories']['status_fixed'] = cur.rowcount
                results['fixed'] += cur.rowcount

                # Fix missing provider
                cur.execute("""
                    UPDATE facilities SET provider = name
                    WHERE provider IS NULL AND name IS NOT NULL
                """)
                results['categories']['provider_fixed'] = cur.rowcount
                results['fixed'] += cur.rowcount

                # Remove empty announcements
                cur.execute("""
                    DELETE FROM announcements
                    WHERE title IS NULL OR title = '' OR length(title) < 10
                """)
                results['categories']['empty_news_removed'] = cur.rowcount

                # Trim whitespace
                cur.execute("""
                    UPDATE facilities
                    SET name = TRIM(name),
                        city = TRIM(city),
                        provider = TRIM(provider)
                    WHERE name LIKE ' %' OR name LIKE '% '
                       OR city LIKE ' %' OR city LIKE '% '
                       OR provider LIKE ' %' OR provider LIKE '% '
                """)
                results['categories']['whitespace_trimmed'] = cur.rowcount
                results['fixed'] += cur.rowcount

                conn.commit()
                cur.close()
            finally:
                conn.close()

            self.state['quality_fixes'] += results['fixed']
            self._save_state()

        except Exception as e:
            logger.error(f"Quality fix error: {e}")
            results['error'] = str(e)

        return results

    def discover_new_apis(self) -> Dict:
        """Discover and test new data APIs"""
        results = {'checked': 0, 'new_found': 0, 'working': [], 'failed': []}

        try:
            import requests

            for source in self.api_sources:
                results['checked'] += 1
                try:
                    resp = requests.get(source['url'], timeout=10)
                    if resp.status_code == 200:
                        results['working'].append(source['name'])
                        logger.debug(f"   API check passed: {source['name']}")
                    else:
                        results['failed'].append({'name': source['name'], 'status': resp.status_code})
                        logger.debug(f"   API check failed: {source['name']} ({resp.status_code})")
                except requests.RequestException as e:
                    results['failed'].append({'name': source['name'], 'error': str(e)})
                    logger.debug(f"   API check error: {source['name']} - {e}")
                except Exception as e:
                    results['failed'].append({'name': source['name'], 'error': str(e)})
                    logger.warning(f"   Unexpected API check error: {source['name']} - {e}")
        except Exception as e:
            logger.error(f"API discovery error: {e}")

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

    def extract_gas_infrastructure_from_news(self) -> Dict:
        """Extract natural gas pipeline and midstream data from news articles"""
        results = {'pipelines': 0, 'midstream': 0, 'lng': 0, 'added': 0}

        try:
            conn = get_db()
            try:
                cur = conn.cursor(cursor_factory=RealDictCursor)

                cur.execute("""
                    SELECT id, title, COALESCE(content, summary, '') AS content, source_url FROM announcements
                    WHERE published_date > NOW() - INTERVAL '7 days'
                    ORDER BY published_date DESC LIMIT 300
                """)
                articles = cur.fetchall()

                # Separate cursor for writes so a failed INSERT (which aborts
                # the txn) can be rolled back without poisoning the read above.
                wcur = conn.cursor()

                for article in articles:
                    text = f"{article['title']} {article['content'] or ''}"

                    # Check for pipeline mentions
                    for pattern in self.gas_patterns:
                        if re.search(pattern, text, re.IGNORECASE):
                            if 'midstream' in text.lower():
                                results['midstream'] += 1
                            elif 'lng' in text.lower():
                                results['lng'] += 1
                            else:
                                results['pipelines'] += 1

                            # Persist the match. gas_pipelines has no NOT NULL
                            # columns, but (name, operator) is the unique key
                            # (gas_pipelines_name_operator_uniq) we conflict on,
                            # so both must be present and stable. Derive name
                            # from the article title; tag operator with a
                            # sentinel so re-runs over the same news dedup
                            # cleanly instead of inserting duplicates each cycle.
                            name = (article['title'] or '').strip()[:200]
                            if name:
                                operator = 'news-extracted'
                                source_id = f"news_gas_{int(hashlib.sha1(str(article['source_url'] or name).encode()).hexdigest()[:12], 16)}"
                                try:
                                    wcur.execute("""
                                        INSERT INTO gas_pipelines
                                        (name, operator, pipeline_type, status, source, source_id)
                                        VALUES (%s, %s, %s, %s, %s, %s)
                                        ON CONFLICT (name, operator) DO NOTHING
                                    """, (name, operator, 'discovered', 'active',
                                          'news_extraction', source_id[:100]))
                                    if wcur.rowcount and wcur.rowcount > 0:
                                        results['added'] += 1
                                    conn.commit()
                                except Exception as ins_err:
                                    conn.rollback()
                                    logger.debug(f"Gas pipeline insert skipped: {ins_err}")
                            break

                wcur.close()
                cur.close()
            finally:
                conn.close()
            self.state['gas_infrastructure_found'] += results['added']
            self._save_state()

        except Exception as e:
            logger.error(f"Gas infrastructure extraction error: {e}")
            results['error'] = str(e)

        return results

    def extract_transmission_infrastructure_from_news(self) -> Dict:
        """Extract power transmission line and substation data from news articles"""
        results = {'transmission_lines': 0, 'hvdc': 0, 'substations': 0, 'added': 0}

        try:
            conn = get_db()
            try:
                cur = conn.cursor(cursor_factory=RealDictCursor)

                cur.execute("""
                    SELECT id, title, COALESCE(content, summary, '') AS content, source_url FROM announcements
                    WHERE published_date > NOW() - INTERVAL '7 days'
                    ORDER BY published_date DESC LIMIT 300
                """)
                articles = cur.fetchall()

                # Separate cursor for writes so a failed INSERT (which aborts
                # the txn) can be rolled back without poisoning the read above.
                wcur = conn.cursor()

                for article in articles:
                    text = f"{article['title']} {article['content'] or ''}"

                    # Check for transmission line mentions
                    for pattern in self.transmission_patterns:
                        if re.search(pattern, text, re.IGNORECASE):
                            if 'hvdc' in text.lower():
                                results['hvdc'] += 1
                            elif 'substation' in text.lower():
                                results['substations'] += 1
                            else:
                                results['transmission_lines'] += 1

                            # Persist the match. transmission_lines' only unique
                            # key is transmission_lines_hifld_id_uniq on
                            # (hifld_id), so we MUST supply a deterministic,
                            # non-null hifld_id (NULLs never conflict → a new
                            # dupe every cycle). Synthesize it from source_url
                            # so the same article dedups across runs.
                            name = (article['title'] or '').strip()[:500]
                            if name:
                                hifld_id = f"news_{int(hashlib.sha1(str(article['source_url'] or name).encode()).hexdigest()[:12], 16)}"
                                try:
                                    wcur.execute("""
                                        INSERT INTO transmission_lines
                                        (hifld_id, name, operator, status, line_type, source)
                                        VALUES (%s, %s, %s, %s, %s, %s)
                                        ON CONFLICT (hifld_id) DO NOTHING
                                    """, (hifld_id[:50], name, 'news-extracted',
                                          'operational', 'discovered', 'news_extraction'))
                                    if wcur.rowcount and wcur.rowcount > 0:
                                        results['added'] += 1
                                    conn.commit()
                                except Exception as ins_err:
                                    conn.rollback()
                                    logger.debug(f"Transmission line insert skipped: {ins_err}")
                            break

                wcur.close()
                cur.close()
            finally:
                conn.close()
            self.state['transmission_infrastructure_found'] += results['added']
            self._save_state()

        except Exception as e:
            logger.error(f"Transmission infrastructure extraction error: {e}")
            results['error'] = str(e)

        return results

    def extract_fiber_infrastructure_from_news(self) -> Dict:
        """Extract dark fiber and lit fiber route data from news articles"""
        results = {'dark_fiber': 0, 'lit_fiber': 0, 'carriers': 0, 'added': 0}

        try:
            conn = get_db()
            try:
                cur = conn.cursor(cursor_factory=RealDictCursor)

                cur.execute("""
                    SELECT id, title, COALESCE(content, summary, '') AS content, source_url FROM announcements
                    WHERE published_date > NOW() - INTERVAL '7 days'
                    ORDER BY published_date DESC LIMIT 300
                """)
                articles = cur.fetchall()

                carriers = ['Zayo', 'Lumen', 'Crown Castle', 'Windstream', 'Level3', 'Cogent']

                # Separate cursor for writes so a failed INSERT (which aborts
                # the txn) can be rolled back without poisoning the read above.
                wcur = conn.cursor()

                for article in articles:
                    text = f"{article['title']} {article['content'] or ''}"
                    text_lc = text.lower()

                    # Check for fiber mentions
                    for pattern in self.fiber_patterns:
                        if re.search(pattern, text, re.IGNORECASE):
                            if 'dark' in text_lc:
                                results['dark_fiber'] += 1
                                route_type = 'dark'
                            elif 'lit' in text_lc:
                                results['lit_fiber'] += 1
                                route_type = 'lit'
                            else:
                                route_type = 'discovered'

                            # Check for carrier mentions → provider
                            provider = 'Unknown'
                            for carrier in carriers:
                                if carrier.lower() in text_lc:
                                    results['carriers'] += 1
                                    provider = carrier
                                    break

                            # Persist the match. fiber_routes' stable unique key
                            # is the inline `source_id TEXT UNIQUE`, so conflict
                            # on (source_id) with a deterministic id derived from
                            # source_url → the same article dedups across runs.
                            name = (article['title'] or '').strip()[:200]
                            if name:
                                source_id = f"news_fiber_{int(hashlib.sha1(str(article['source_url'] or name).encode()).hexdigest()[:12], 16)}"
                                try:
                                    wcur.execute("""
                                        INSERT INTO fiber_routes
                                        (name, provider, route_type, status, source, source_id)
                                        VALUES (%s, %s, %s, %s, %s, %s)
                                        ON CONFLICT (source_id) DO NOTHING
                                    """, (name, provider[:100], route_type, 'active',
                                          'news_extraction', source_id[:100]))
                                    if wcur.rowcount and wcur.rowcount > 0:
                                        results['added'] += 1
                                    conn.commit()
                                except Exception as ins_err:
                                    conn.rollback()
                                    logger.debug(f"Fiber route insert skipped: {ins_err}")
                            break

                wcur.close()
                cur.close()
            finally:
                conn.close()
            self.state['fiber_infrastructure_found'] += results['added']
            self._save_state()

        except Exception as e:
            logger.error(f"Fiber infrastructure extraction error: {e}")
            results['error'] = str(e)

        return results

    def extract_substation_infrastructure_from_news(self) -> Dict:
        """Extract electrical substation data from news articles"""
        results = {'substations': 0, 'transformers': 0, 'voltage_levels': 0, 'added': 0}

        try:
            conn = get_db()
            try:
                cur = conn.cursor(cursor_factory=RealDictCursor)

                cur.execute("""
                    SELECT id, title, COALESCE(content, summary, '') AS content, source_url FROM announcements
                    WHERE published_date > NOW() - INTERVAL '7 days'
                    ORDER BY published_date DESC LIMIT 300
                """)
                articles = cur.fetchall()

                for article in articles:
                    text = f"{article['title']} {article['content'] or ''}"

                    # Check for substation mentions
                    for pattern in self.substation_patterns:
                        if re.search(pattern, text, re.IGNORECASE):
                            if 'transformer' in text.lower():
                                results['transformers'] += 1
                            elif re.search(r'\d+\s*kV', text, re.IGNORECASE):
                                results['voltage_levels'] += 1
                            else:
                                results['substations'] += 1
                            break

                cur.close()
            finally:
                conn.close()
            self.state['substation_infrastructure_found'] += results['added']
            self._save_state()

        except Exception as e:
            logger.error(f"Substation infrastructure extraction error: {e}")

        return results

    def extract_power_plants_from_news(self) -> Dict:
        """Extract power plant data from news articles"""
        results = {'natural_gas': 0, 'nuclear': 0, 'solar': 0, 'wind': 0, 'added': 0}

        try:
            conn = get_db()
            try:
                cur = conn.cursor(cursor_factory=RealDictCursor)

                cur.execute("""
                    SELECT id, title, COALESCE(content, summary, '') AS content, source_url FROM announcements
                    WHERE published_date > NOW() - INTERVAL '7 days'
                    ORDER BY published_date DESC LIMIT 300
                """)
                articles = cur.fetchall()

                for article in articles:
                    text = f"{article['title']} {article['content'] or ''}"

                    # Check for power plant mentions
                    for pattern in self.power_plant_patterns:
                        if re.search(pattern, text, re.IGNORECASE):
                            if 'gas' in text.lower():
                                results['natural_gas'] += 1
                            elif 'nuclear' in text.lower():
                                results['nuclear'] += 1
                            elif 'solar' in text.lower():
                                results['solar'] += 1
                            elif 'wind' in text.lower():
                                results['wind'] += 1
                            break

                cur.close()
            finally:
                conn.close()
            self.state['power_plant_infrastructure_found'] += results['added']
            self._save_state()

        except Exception as e:
            logger.error(f"Power plant extraction error: {e}")

        return results

    def extract_infrastructure_from_news(self) -> Dict:
        """Extract infrastructure data from news articles"""
        results = {'fiber_mentions': 0, 'power_mentions': 0, 'land_mentions': 0, 'added': 0}

        try:
            conn = get_db()
            try:
                cur = conn.cursor(cursor_factory=RealDictCursor)

                cur.execute("""
                    SELECT id, title, COALESCE(content, summary, '') AS content, source_url FROM announcements
                    WHERE published_date > NOW() - INTERVAL '3 days'
                    ORDER BY published_date DESC LIMIT 200
                """)
                articles = cur.fetchall()

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
                            break

                cur.close()
            finally:
                conn.close()

        except Exception as e:
            logger.error(f"Infrastructure news extraction error: {e}")
            results['error'] = str(e)

        return results

    def _has_new_announcements(self) -> tuple[bool, int]:
        """r49.5 (2026-05-25): cheap pre-flight check — skip the full
        cycle (capacity + deals + 8 other extractors, ~25 sec total)
        when no new news has landed since last run. State stored in
        brain_state.last_processed_announcement_id. With cron firing
        every 5 min and news ingesting 6 feeds every ~30 min, ~5 of 6
        cycles previously did 25 seconds of work to find 0 new rows.
        This drops idle-cycle cost from 25s → ~150ms."""
        try:
            conn = get_db()
            try:
                cur = conn.cursor()
                # 2026-05-30 WATERMARK FIX (round 2): the prior COUNT(*) watermark
                # is NON-MONOTONIC. auto_fix_quality_issues() runs
                #   DELETE FROM announcements WHERE length(title) < 10
                # every cycle, so when deletes ≈ inserts the count stalls and the
                # gate "max_id > last_id" stays False forever → the brain SKIPS
                # every cycle again (same flat-line failure mode as the r43-H
                # int(MAX(id)) bug). Use MAX(discovered_at) epoch instead.
                #
                # Why discovered_at (verified against live DB 2026-05-30):
                #   - announcements has NO created_at column; id is a TEXT md5 PK.
                #   - discovered_at is set to NOW() on every INSERT *and* every
                #     ON CONFLICT DO UPDATE (see main.py news ingest), so it is
                #     strictly monotonic — new/re-seen rows always push it forward,
                #     and DELETEs can never lower an existing MAX.
                #   - It is stored as TEXT but is 100% clean (0/9544 null/malformed)
                #     and casts to timestamptz (infrastructure_discovery.py already
                #     does discovered_at::timestamptz).
                #   - published_date is rejected: it is feed-provided and contained
                #     FUTURE dates (MAX = 2026-06-30) that would pin the watermark
                #     ahead and re-break the gate.
                # The CASE-regex guard means a malformed row is treated as NULL
                # rather than raising, and COALESCE(...,0) handles the empty table —
                # so this query CANNOT throw, preserving the fail-open below. The
                # epoch is a monotonically increasing bigint, which slots straight
                # into the existing integer stored-watermark plumbing.
                cur.execute(
                    "SELECT COALESCE(EXTRACT(EPOCH FROM MAX("
                    "  CASE WHEN discovered_at ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}' "
                    "       THEN discovered_at::timestamptz END))::bigint, 0) "
                    "FROM announcements"
                )
                _raw_max = (cur.fetchone() or [0])[0]
                try:
                    max_id = int(_raw_max) if _raw_max is not None else 0
                except (TypeError, ValueError):
                    max_id = 0
                cur.execute("SELECT state_value FROM brain_state "
                            "WHERE state_key = 'last_processed_announcement_id'")
                row = cur.fetchone()
                last_id = 0
                if row and row[0] is not None:
                    try:
                        last_id = int(row[0]) if isinstance(row[0], (int, str)) else int(row[0].get('id', 0))
                    except Exception:
                        last_id = 0
                cur.close()
                return (max_id > last_id, max_id)
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"_has_new_announcements pre-flight failed: {e}")
            return (True, 0)  # On error, do the full cycle — fail-open

    def _save_last_processed_announcement(self, max_id: int) -> None:
        try:
            conn = get_db()
            try:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO brain_state (state_key, state_value, updated_at)
                    VALUES ('last_processed_announcement_id', %s::jsonb, CURRENT_TIMESTAMP)
                    ON CONFLICT (state_key) DO UPDATE
                       SET state_value = EXCLUDED.state_value,
                           updated_at  = CURRENT_TIMESTAMP
                """, (str(max_id),))
                conn.commit()
                cur.close()
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"_save_last_processed_announcement failed: {e}")

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
            'gas_infrastructure': {},
            'transmission_infrastructure': {},
            'fiber_infrastructure': {},
            'substation_infrastructure': {},
            'power_plants': {},
            'apis': {},
        }

        # r49.5: skip the full cycle if no new announcements since last run.
        # Saves ~25s of CPU per skipped cycle. Quality/API checks still run
        # (they don't depend on news), so the brain stays alive — just not
        # re-scanning the same 500 articles every 5 minutes.
        has_new, current_max_id = self._has_new_announcements()
        if not has_new:
            elapsed_ms = int((datetime.now() - start_time).total_seconds() * 1000)
            logger.info(f"Autonomous Brain - Cycle {results['cycle_id']} skipped "
                        f"(no new announcements since id={current_max_id}, "
                        f"pre-flight {elapsed_ms}ms)")
            results.update({
                'skipped': True,
                'skip_reason': 'no_new_announcements',
                'last_processed_announcement_id': current_max_id,
                'pre_flight_ms': elapsed_ms,
            })
            self.state['total_cycles'] += 1
            # 2026-05-30 FREEZE VISIBILITY: previously the skip path bumped
            # total_cycles but left last_cycle/autonomous_actions untouched, so a
            # paused or flat-lined brain looked identical to "healthy_quiet" on
            # /api/autonomous/status and /heartbeat. Mirror the full-run path's
            # last_cycle write and log an explicit 'skipped' action so a stalled
            # brain is visibly stalled (and the action stream shows why).
            now_iso = datetime.now().isoformat()
            self.state['last_cycle'] = now_iso
            skip_action = {
                'type': 'skipped',
                'reason': 'no_new_announcements',
                'ts': now_iso,
                'cycle': results['cycle_id'],
                'last_processed_announcement_id': current_max_id,
            }
            self.state['autonomous_actions'] = (
                self.state.get('autonomous_actions', [])[-99:] + [skip_action]
            )
            self._save_state()
            return results

        logger.info(f"Autonomous Brain - Cycle {results['cycle_id']} starting... "
                    f"(news_max_id={current_max_id})")

        try:
            results['capacity'] = self.extract_capacity_from_news()
            logger.info(f"   Capacity: {results['capacity'].get('new_pipeline', 0)} new entries")
        except Exception as e:
            logger.error(f"   Capacity extraction failed: {e}")

        try:
            results['deals'] = self.extract_deals_from_news()
            logger.info(f"   Deals: {results['deals'].get('deals_found', 0)} found")
        except Exception as e:
            logger.error(f"   Deal extraction failed: {e}")

        try:
            results['quality'] = self.auto_fix_quality_issues()
            logger.info(f"   Quality: {results['quality'].get('fixed', 0)} fixes")
        except Exception as e:
            logger.error(f"   Quality fixes failed: {e}")

        try:
            results['infrastructure'] = self.extract_infrastructure_from_news()
            logger.info(f"   Infrastructure: {results['infrastructure'].get('fiber_mentions', 0)} fiber, {results['infrastructure'].get('power_mentions', 0)} power mentions")
        except Exception as e:
            logger.error(f"   Infrastructure extraction failed: {e}")

        try:
            results['gas_infrastructure'] = self.extract_gas_infrastructure_from_news()
            logger.info(f"   Gas: {results['gas_infrastructure'].get('pipelines', 0)} pipelines, {results['gas_infrastructure'].get('midstream', 0)} midstream, {results['gas_infrastructure'].get('lng', 0)} LNG")
        except Exception as e:
            logger.error(f"   Gas infrastructure extraction failed: {e}")

        try:
            results['transmission_infrastructure'] = self.extract_transmission_infrastructure_from_news()
            logger.info(f"   Transmission: {results['transmission_infrastructure'].get('transmission_lines', 0)} lines, {results['transmission_infrastructure'].get('hvdc', 0)} HVDC, {results['transmission_infrastructure'].get('substations', 0)} substations")
        except Exception as e:
            logger.error(f"   Transmission infrastructure extraction failed: {e}")

        try:
            results['fiber_infrastructure'] = self.extract_fiber_infrastructure_from_news()
            logger.info(f"   Fiber: {results['fiber_infrastructure'].get('dark_fiber', 0)} dark, {results['fiber_infrastructure'].get('lit_fiber', 0)} lit, {results['fiber_infrastructure'].get('carriers', 0)} carriers")
        except Exception as e:
            logger.error(f"   Fiber infrastructure extraction failed: {e}")

        try:
            results['substation_infrastructure'] = self.extract_substation_infrastructure_from_news()
            logger.info(f"   Substations: {results['substation_infrastructure'].get('substations', 0)} found, {results['substation_infrastructure'].get('transformers', 0)} transformers")
        except Exception as e:
            logger.error(f"   Substation infrastructure extraction failed: {e}")

        try:
            results['power_plants'] = self.extract_power_plants_from_news()
            logger.info(f"   Power Plants: {results['power_plants'].get('natural_gas', 0)} gas, {results['power_plants'].get('solar', 0)} solar, {results['power_plants'].get('wind', 0)} wind")
        except Exception as e:
            logger.error(f"   Power plant extraction failed: {e}")

        if self.state['total_cycles'] % 10 == 0:
            try:
                infra_sync = self.sync_infrastructure()
                results['infrastructure']['sync'] = infra_sync
                logger.info(f"   Infra Sync: {infra_sync.get('fiber', 0)} fiber, {infra_sync.get('substations', 0)} substations")
            except Exception as e:
                logger.error(f"   Infrastructure sync failed: {e}")

        if self.state['total_cycles'] % 10 == 0:
            try:
                results['apis'] = self.discover_new_apis()
                logger.info(f"   APIs: {len(results['apis'].get('working', []))} working")
            except Exception as e:
                logger.error(f"   API discovery failed: {e}")

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

        # r49.5: persist the cursor so the next cycle can skip if no new
        # announcements have arrived (saves 25s of CPU per idle cycle).
        if current_max_id > 0:
            self._save_last_processed_announcement(current_max_id)

        duration = (datetime.now() - start_time).total_seconds()
        results['duration_seconds'] = duration
        logger.info(f"Autonomous Brain - Cycle {results['cycle_id']} complete in {duration:.1f}s")

        # Phase ZZZZZ-round6c (2026-05-23): write each sub-extractor's
        # result to extraction_intelligence so the autonomous-intelligence
        # dashboard at /api/v1/extractor-brain/insights and /brain/innovation
        # reports more than just "github-actions-data-pulse" as the only
        # active source. Each of the 9 domain extractors below becomes its
        # own source_id with success/failure + rows ingested per cycle.
        try:
            self._record_cycle_to_extraction_intelligence(results, duration)
        except Exception as e:
            logger.debug(f"   (non-fatal) extraction_intelligence write failed: {e}")

        # r47.39 (2026-05-26): inline heartbeat. The Phase 92 wrapper at
        # line ~1289 wraps `run_brain_cycle` from globals(), but the
        # actual cycle runs through this method on the class instance —
        # the wrapper never sees it. Fire directly so the source-registry
        # `backend-autonomous-brain` row stops showing "never ran".
        try:
            from dchub_heartbeat import heartbeat as _hb
            rows = int(results.get("total_new_rows", 0) or 0)
            _hb(
                "backend-autonomous-brain",
                status="success",
                rows_affected=rows,
                duration_ms=int(duration * 1000),
                metadata={"cycle_id": results.get("cycle_id")},
            )
        except Exception:
            pass  # best-effort, never blocks the cycle

        return results

    def _record_cycle_to_extraction_intelligence(self, results: Dict, duration: float):
        """Phase ZZZZZ-round6c: instrument each domain extractor's output
        into the extraction_intelligence table so the brain dashboard
        actually has more than 1 active source."""
        # Map: domain key in results → source_id + canonical rows-count getter.
        #
        # rows_key MUST be the real committed-insert count, NOT a per-cycle
        # regex sub-bucket counter (those re-zero every cycle and increment on
        # every pattern match regardless of whether a row was actually written,
        # so the dashboard showed inflated noise — or, post INSERT-fix, 0 — for
        # the gas/transmission/fiber domains). Use 'added' (the dedup-guarded
        # rowcount>0 tally) for those three. capacity('new_pipeline') and
        # deals('deals_found') are already only incremented inside their
        # committed-INSERT branches, so they are the real counts.
        #
        # substation_infrastructure + power_plants are intentionally DROPPED:
        # their extractors have NO INSERT (they only re-scan articles each
        # cycle), so their counters are pure per-cycle inflation and they can
        # never write a row. That data is sourced from HIFLD/EIA elsewhere, not
        # article-extracted, so they don't belong in the extraction dashboard.
        domains = [
            ('capacity',                   'autonomous-brain-capacity',     'new_pipeline'),
            ('deals',                      'autonomous-brain-deals',        'deals_found'),
            ('quality',                    'autonomous-brain-quality',      'fixed'),
            ('infrastructure',             'autonomous-brain-infra-news',   'fiber_mentions'),
            ('gas_infrastructure',         'autonomous-brain-gas',          'added'),
            ('transmission_infrastructure','autonomous-brain-transmission', 'added'),
            ('fiber_infrastructure',       'autonomous-brain-fiber',        'added'),
        ]
        try:
            import os, psycopg2
            db = os.environ.get('DATABASE_URL')
            if not db:
                return
            conn = psycopg2.connect(db, connect_timeout=4)
            try:
                with conn.cursor() as cur:
                    # Ensure table exists (idempotent — _ensure_tables on the
                    # routes/extractor_brain.py side does the heavy lifting,
                    # but a redundant CREATE IF NOT EXISTS here means cold
                    # boots don't drop rows.)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS extraction_intelligence (
                            id              SERIAL PRIMARY KEY,
                            source_id       TEXT NOT NULL,
                            outcome         TEXT NOT NULL,
                            rows_inserted   INTEGER DEFAULT 0,
                            duration_ms     INTEGER DEFAULT 0,
                            error           TEXT,
                            anomaly_score   REAL DEFAULT 0,
                            observations    TEXT,
                            proposed_fix    TEXT,
                            observed_at     TIMESTAMPTZ DEFAULT NOW()
                        )
                    """)
                    per_domain_ms = max(int(duration * 1000 / max(len(domains), 1)), 1)
                    for key, source_id, rows_key in domains:
                        dr = results.get(key) or {}
                        rows = int(dr.get(rows_key) or 0)
                        # Outcome honesty: 'success' ONLY when a row was actually
                        # written this cycle. A non-empty result dict with 0 rows
                        # is a no-op (nothing new in the article feed), not a
                        # success — report it as 'idle' so the dashboard stops
                        # showing healthy 100% for extractors that wrote nothing.
                        # If the extractor surfaced an exception (it stores the
                        # message under an 'error' key), record it in the error
                        # column and mark the run as failed.
                        err = dr.get('error')
                        if err:
                            outcome = 'error'
                        elif rows > 0:
                            outcome = 'success'
                        else:
                            outcome = 'idle'
                        cur.execute("""
                            INSERT INTO extraction_intelligence
                                (source_id, outcome, rows_inserted, duration_ms, error, observations)
                            VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
                        """, (source_id, outcome, rows, per_domain_ms,
                              (str(err)[:2000] if err else None),
                              __import__('json').dumps({"cycle": results.get('cycle_id')})))
                    conn.commit()
            finally:
                try: conn.close()
                except Exception: pass
        except Exception as e:
            logger.debug(f"   extraction_intelligence write skipped: {e}")
            return

    def _extract_operator(self, text: str) -> str:
        """Extract operator name from text - expanded list of 60+ operators"""
        known_operators = [
            # Major REIT operators
            'Equinix', 'Digital Realty', 'CyrusOne', 'QTS', 'CoreSite',
            'Vantage', 'DataBank', 'Flexential', 'Switch', 'NTT',
            # Hyperscale cloud operators
            'Microsoft', 'Amazon', 'Google', 'Meta', 'Apple',
            'AWS', 'Azure', 'GCP', 'Oracle', 'IBM',
            # Private equity / infrastructure operators
            'Stack Infrastructure', 'Prime Data Centers', 'EdgeCore',
            'Applied Digital', 'Compass', 'Stream Data Centers',
            # AI-focused operators
            'Lambda Labs', 'CoreWeave', 'Lambda', 'Crusoe',
            # Regional and specialized operators
            'CloudHQ', 'Aligned', 'Yondr', 'Lincoln Rackhouse', 'T5',
            'Sabey', 'TierPoint', 'H5', 'Chirisa', 'PowerHouse',
            'Novva', 'Skybox', 'DP Facilities', 'Nautilus', 'Green Mountain',
            'Iron Mountain', 'Cologix', 'Cyxtera', 'Evoque', 'US Signal',
            'PhoenixNAP', 'Hivelocity', 'OVHcloud', 'Scaleway', 'Interxion',
            'Zenlayer', 'Vultr', 'Linode', 'Packet', 'Joyent',
            'Rackspace', 'Liquid Web', 'Peak Hosting', 'Internap', 'Peer1',
            'Softlayer', 'ServePath', 'CenturyLink', 'Level3', 'Cogent',
            'Sentient', 'AirTrunk', 'DataCentred', 'Telehouse', 'Easynet',
            'Host Europe', 'GTXcel', 'EODC', 'VersaWeb', 'QuintelGroup',
        ]

        for op in known_operators:
            if op.lower() in text.lower():
                return op

        return 'Unknown'

    def _extract_location(self, text: str) -> str:
        """Extract location from text - expanded to 60+ major markets"""
        # US markets
        us_locations = [
            'Ashburn', 'Virginia', 'VA',
            'Dallas', 'Texas', 'TX', 'DFW', 'Fort Worth',
            'Phoenix', 'Arizona', 'AZ',
            'Silicon Valley', 'San Jose', 'California', 'CA',
            'Chicago', 'Illinois', 'IL',
            'New York', 'New Jersey', 'NJ', 'NYC', 'Jersey City',
            'Atlanta', 'Georgia', 'GA',
            'Denver', 'Colorado', 'CO',
            'Las Vegas', 'Nevada', 'NV',
            'Los Angeles', 'LA',
            'Portland', 'Oregon', 'OR',
            'Seattle', 'Washington', 'WA',
            'Miami', 'Florida', 'FL',
            'Houston', 'TX',
            'Minneapolis', 'Minnesota', 'MN',
            'St. Louis', 'Missouri', 'MO',
            'Kansas City', 'Kansas', 'KS',
            'Austin', 'San Antonio',
            'Boston', 'Massachusetts', 'MA',
            'Philadelphia', 'Pennsylvania', 'PA',
            'Nashville', 'Tennessee', 'TN',
            'Memphis', 'Mississippi', 'MS',
            'Salt Lake City', 'Utah', 'UT',
            'Raleigh', 'Durham', 'North Carolina', 'NC',
            'Charlotte', 'South Carolina', 'SC',
            'Detroit', 'Michigan', 'MI',
            'Columbus', 'Ohio', 'OH',
            'Cleveland', 'Cincinnati',
            'Indianapolis', 'Indiana', 'IN',
            'Louisville', 'Kentucky', 'KY',
            'Milwaukee', 'Wisconsin', 'WI',
        ]

        # International markets
        intl_locations = [
            'Dublin', 'Ireland',
            'Zurich', 'Switzerland',
            'London', 'UK', 'United Kingdom',
            'Frankfurt', 'Germany',
            'Amsterdam', 'Netherlands',
            'Paris', 'France',
            'Madrid', 'Spain',
            'Milan', 'Italy',
            'Stockholm', 'Sweden',
            'Helsinki', 'Finland',
            'Marseille', 'France',
            'Warsaw', 'Poland',
            'Copenhagen', 'Denmark',
            'Tokyo', 'Japan',
            'Osaka', 'Japan',
            'Sydney', 'Australia',
            'Melbourne', 'Australia',
            'Singapore',
            'Mumbai', 'India',
            'Delhi', 'India',
            'Seoul', 'South Korea',
            'Bangkok', 'Thailand',
            'Jakarta', 'Indonesia',
            'Manila', 'Philippines',
            'Hong Kong',
            'Shanghai', 'China',
            'Beijing', 'China',
            'São Paulo', 'Brazil',
            'Rio de Janeiro', 'Brazil',
            'Santiago', 'Chile',
            'Mexico City', 'Mexico',
            'Toronto', 'Canada',
            'Vancouver', 'Canada',
            'Johannesburg', 'South Africa',
            'Nairobi', 'Kenya',
            'Cairo', 'Egypt',
            'Dubai', 'UAE',
            'Tel Aviv', 'Israel',
        ]

        all_locations = us_locations + intl_locations

        for loc in all_locations:
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
            'gas_infrastructure_found': self.state.get('gas_infrastructure_found', 0),
            'transmission_infrastructure_found': self.state.get('transmission_infrastructure_found', 0),
            'fiber_infrastructure_found': self.state.get('fiber_infrastructure_found', 0),
            'substation_infrastructure_found': self.state.get('substation_infrastructure_found', 0),
            'power_plant_infrastructure_found': self.state.get('power_plant_infrastructure_found', 0),
            'last_cycle': self.state.get('last_cycle'),
            'recent_actions': self.state.get('autonomous_actions', [])[-10:],
        }

    def start_scheduler(self, interval_seconds: int = 300):
        """Start the autonomous scheduler"""
        if self.running:
            logger.info("Autonomous Brain already running")
            return

        self.running = True
        self._stop_event = threading.Event()

        def run_loop():
            logger.info(f"Autonomous Brain loop started")
            while self.running and not self._stop_event.is_set():
                try:
                    # r65 leader-lock self-heal: re-check LIVE leadership each
                    # cycle. The advisory lock can drop/transfer at runtime, so a
                    # demoted ex-leader must STOP singleton work and a promoted
                    # follower must start. Lazy import dodges the circular import.
                    _is_leader = True
                    try:
                        from main import is_current_leader as _icl
                        _is_leader = _icl()
                    except Exception:
                        _is_leader = True  # can't import → fail OPEN (assume leader)
                    if not _is_leader:
                        logger.info("⏸️ Autonomous cycle skipped — not current leader")
                        self._stop_event.wait(interval_seconds)
                        continue
                    self.run_autonomous_cycle()
                except psycopg2.Error as e:
                    logger.error(f"Autonomous cycle database error: {e}")
                except Exception as e:
                    logger.error(f"Autonomous cycle error: {e}", exc_info=True)

                self._stop_event.wait(interval_seconds)
            logger.info("Autonomous Brain loop stopped")

        self.scheduler_thread = threading.Thread(target=run_loop, daemon=True, name="AutonomousBrain")
        self.scheduler_thread.start()
        logger.info(f"Autonomous Brain started - running every {interval_seconds}s")

    def stop_scheduler(self):
        """Stop the autonomous scheduler"""
        self.running = False
        if hasattr(self, '_stop_event'):
            self._stop_event.set()
        logger.info("Autonomous Brain stopped")


from flask import Blueprint, jsonify, request

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

# === phase 92: source-registry heartbeat ===
# Wraps run_brain_cycle to ping heartbeat after each cycle.
_phase92_heartbeat_registered = True
try:
    from dchub_heartbeat import heartbeat as _phase92_heartbeat
    if 'run_brain_cycle' in globals() and callable(globals()['run_brain_cycle']):
        _orig_run_brain_cycle = globals()['run_brain_cycle']
        import time as _phase92_time, functools as _phase92_functools
        @_phase92_functools.wraps(_orig_run_brain_cycle)
        def _phase92_wrapped(*a, **kw):
            _started = _phase92_time.time()
            try:
                result = _orig_run_brain_cycle(*a, **kw)
                _phase92_heartbeat("backend-autonomous-brain", status="success",
                                  duration_ms=int((_phase92_time.time() - _started) * 1000))
                return result
            except Exception as _e:
                _phase92_heartbeat("backend-autonomous-brain", status="failure",
                                  duration_ms=int((_phase92_time.time() - _started) * 1000),
                                  error=f"{type(_e).__name__}: {_e}")
                raise
        globals()['run_brain_cycle'] = _phase92_wrapped
except Exception:
    pass
