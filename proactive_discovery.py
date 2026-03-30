"""
Proactive Discovery Engine
Automatically searches for fiber KMZ files and utility data sources across the web.
Runs autonomously to find and ingest new infrastructure data.
"""

import requests
import re
import os
import time
import hashlib
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import threading
import logging
from flask import Blueprint, jsonify, request
from db_utils import get_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class ProactiveDiscoveryEngine:
    def __init__(self, db_path: str = 'dc_nexus.db'):
        self.db_path = db_path
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'DC-Hub-Nexus/1.0 (Data Center Intelligence Platform)'
        })
        
        self.fiber_search_sources = [
            {'name': 'GitHub', 'search_url': 'https://github.com/search?q=fiber+network+kmz+OR+kml&type=repositories', 'type': 'code_repo'},
            {'name': 'GitHub GeoJSON', 'search_url': 'https://github.com/search?q=telecom+fiber+geojson&type=code', 'type': 'code_repo'},
            {'name': 'OpenDataSoft', 'search_url': 'https://data.opendatasoft.com/explore/?q=fiber+network', 'type': 'open_data'},
            {'name': 'Data.gov', 'search_url': 'https://catalog.data.gov/dataset?q=fiber+optic', 'type': 'gov_data'},
            {'name': 'ArcGIS Hub', 'search_url': 'https://hub.arcgis.com/search?q=fiber%20network', 'type': 'gis'},
            {'name': 'NTIA Broadband', 'search_url': 'https://broadbandmap.fcc.gov/', 'type': 'gov_data'},
        ]
        
        self.utility_search_sources = [
            {'name': 'EIA Power Plants', 'url': 'https://www.eia.gov/electricity/data/eia860/', 'type': 'energy'},
            {'name': 'OpenStreetMap Power', 'url': 'https://overpass-api.de/api/interpreter', 'type': 'osm'},
            {'name': 'HIFLD Substations', 'url': 'https://hifld-geoplatform.opendata.arcgis.com/datasets/electric-substations', 'type': 'gov_data'},
            {'name': 'GridStatus', 'url': 'https://www.gridstatus.io/', 'type': 'grid'},
            {'name': 'Permit Data', 'url': 'https://data.gov/search?q=building+permits+data+center', 'type': 'permits'},
        ]
        
        self.telecom_providers = [
            'Zayo', 'Lumen', 'Crown Castle', 'AT&T', 'Verizon', 'Cogent',
            'Level3', 'GTT', 'Telia', 'NTT', 'Equinix', 'Digital Realty',
            'CoreSite', 'CyrusOne', 'QTS', 'Flexential', 'DataBank',
            'Connected Nation', 'NTCA', 'ACA Connects'
        ]
        
        self._init_db()
        
    def _init_db(self):
        conn = get_db(self.db_path)
        c = conn.cursor()
        
        c.execute('''CREATE TABLE IF NOT EXISTS discovered_fiber_sources (
            id SERIAL PRIMARY KEY,
            source_name TEXT,
            source_url TEXT UNIQUE,
            source_type TEXT,
            file_type TEXT,
            provider TEXT,
            region TEXT,
            file_count INTEGER DEFAULT 0,
            last_checked TEXT,
            download_url TEXT,
            status TEXT DEFAULT 'pending',
            discovered_at TEXT DEFAULT CURRENT_TIMESTAMP
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS discovered_utility_sources (
            id SERIAL PRIMARY KEY,
            source_name TEXT,
            source_url TEXT UNIQUE,
            data_type TEXT,
            coverage_area TEXT,
            record_count INTEGER DEFAULT 0,
            last_updated TEXT,
            api_available INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',
            discovered_at TEXT DEFAULT CURRENT_TIMESTAMP
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS discovery_runs (
            id SERIAL PRIMARY KEY,
            run_type TEXT,
            started_at TEXT,
            completed_at TEXT,
            sources_checked INTEGER DEFAULT 0,
            new_sources_found INTEGER DEFAULT 0,
            files_downloaded INTEGER DEFAULT 0,
            status TEXT DEFAULT 'running'
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS downloaded_files (
            id SERIAL PRIMARY KEY,
            source_id INTEGER,
            file_url TEXT UNIQUE,
            file_name TEXT,
            file_type TEXT,
            file_size INTEGER,
            local_path TEXT,
            processed INTEGER DEFAULT 0,
            routes_extracted INTEGER DEFAULT 0,
            downloaded_at TEXT DEFAULT CURRENT_TIMESTAMP
        )''')
        
        conn.commit()
        conn.close()
        
    def run_fiber_discovery(self) -> Dict:
        """Proactively search for fiber KMZ/KML files across the web"""
        logger.info("Starting proactive fiber discovery...")
        
        conn = get_db(self.db_path)
        c = conn.cursor()
        
        c.execute('''INSERT INTO discovery_runs (run_type, started_at, status) 
                     VALUES ('fiber', %s, 'running') ON CONFLICT (run_type) DO UPDATE SET started_at = EXCLUDED.started_at, status = EXCLUDED.status''', (datetime.now().isoformat(),))
        run_id = c.lastrowid
        conn.commit()
        
        results = {
            'sources_checked': 0,
            'new_sources': [],
            'kmz_files_found': [],
            'kml_files_found': [],
            'geojson_files_found': []
        }
        
        results = self._search_github_fiber(results)
        results = self._search_arcgis_hub(results)
        results = self._search_data_gov(results)
        results = self._search_osm_fiber(results)
        results = self._search_provider_sites(results)
        
        c.execute('''UPDATE discovery_runs SET 
                     completed_at = ?, sources_checked = ?, new_sources_found = ?, status = 'completed'
                     WHERE id = %s''', 
                  (datetime.now().isoformat(), results['sources_checked'], 
                   len(results['new_sources']), run_id))
        conn.commit()
        conn.close()
        
        logger.info(f"Fiber discovery complete: {len(results['new_sources'])} new sources found")
        return results
        
    def _search_github_fiber(self, results: Dict) -> Dict:
        """Search GitHub for fiber network KMZ/KML files"""
        try:
            search_terms = [
                'fiber network kmz', 'fiber optic kml', 'telecom network geojson',
                'broadband infrastructure kmz', 'dark fiber map'
            ]
            
            for term in search_terms:
                try:
                    url = f"https://api.github.com/search/repositories?q={term.replace(' ', '+')}&per_page=10"
                    resp = self.session.get(url, timeout=15)
                    results['sources_checked'] += 1
                    
                    if resp.status_code == 200:
                        data = resp.json()
                        for repo in data.get('items', [])[:5]:
                            source = {
                                'name': repo.get('full_name'),
                                'url': repo.get('html_url'),
                                'type': 'github_repo',
                                'description': repo.get('description', ''),
                                'files': []
                            }
                            
                            contents_url = f"https://api.github.com/repos/{repo.get('full_name')}/contents"
                            try:
                                contents_resp = self.session.get(contents_url, timeout=10)
                                if contents_resp.status_code == 200:
                                    for item in contents_resp.json():
                                        name = item.get('name', '').lower()
                                        if any(ext in name for ext in ['.kmz', '.kml', '.geojson']):
                                            source['files'].append({
                                                'name': item.get('name'),
                                                'download_url': item.get('download_url'),
                                                'size': item.get('size')
                                            })
                            except:
                                pass
                                
                            if source['files']:
                                results['new_sources'].append(source)
                                self._save_fiber_source(source)
                                
                    time.sleep(1)
                except Exception as e:
                    logger.debug(f"GitHub search error: {e}")
                    
        except Exception as e:
            logger.error(f"GitHub fiber search failed: {e}")
            
        return results
        
    def _search_arcgis_hub(self, results: Dict) -> Dict:
        """Search ArcGIS Hub for fiber/telecom datasets"""
        try:
            search_url = "https://hub.arcgis.com/api/v3/datasets"
            params = {
                'q': 'fiber network OR telecom infrastructure',
                'per_page': 20
            }
            
            resp = self.session.get(search_url, params=params, timeout=15)
            results['sources_checked'] += 1
            
            if resp.status_code == 200:
                data = resp.json()
                for dataset in data.get('data', []):
                    attrs = dataset.get('attributes', {})
                    source = {
                        'name': attrs.get('name', 'Unknown'),
                        'url': attrs.get('landingPage', ''),
                        'type': 'arcgis',
                        'description': attrs.get('description', ''),
                        'record_count': attrs.get('recordCount', 0)
                    }
                    
                    if any(kw in source['name'].lower() for kw in ['fiber', 'telecom', 'broadband', 'network']):
                        results['new_sources'].append(source)
                        self._save_fiber_source(source)
                        
        except Exception as e:
            logger.debug(f"ArcGIS search error: {e}")
            
        return results
        
    def _search_data_gov(self, results: Dict) -> Dict:
        """Search Data.gov for fiber/telecom datasets"""
        try:
            search_url = "https://catalog.data.gov/api/3/action/package_search"
            queries = ['fiber optic', 'broadband infrastructure', 'telecommunications network']
            
            for query in queries:
                try:
                    params = {'q': query, 'rows': 10}
                    resp = self.session.get(search_url, params=params, timeout=15)
                    results['sources_checked'] += 1
                    
                    if resp.status_code == 200:
                        data = resp.json()
                        for pkg in data.get('result', {}).get('results', []):
                            for resource in pkg.get('resources', []):
                                fmt = resource.get('format', '').lower()
                                if fmt in ['kmz', 'kml', 'geojson', 'shapefile']:
                                    source = {
                                        'name': pkg.get('title', 'Unknown'),
                                        'url': resource.get('url', ''),
                                        'type': 'data_gov',
                                        'file_type': fmt,
                                        'description': pkg.get('notes', '')
                                    }
                                    results['new_sources'].append(source)
                                    self._save_fiber_source(source)
                                    
                    time.sleep(0.5)
                except Exception as e:
                    logger.debug(f"Data.gov search error: {e}")
                    
        except Exception as e:
            logger.error(f"Data.gov search failed: {e}")
            
        return results
        
    def _search_osm_fiber(self, results: Dict) -> Dict:
        """Search OpenStreetMap for fiber infrastructure"""
        try:
            overpass_url = "https://overpass-api.de/api/interpreter"
            
            query = '''
            [out:json][timeout:30];
            (
              way["telecom"="data_center"];
              way["utility"="fibre_optic_cable"];
              node["telecom"="exchange"];
            );
            out center meta;
            '''
            
            resp = self.session.post(overpass_url, data={'data': query}, timeout=45)
            results['sources_checked'] += 1
            
            if resp.status_code == 200:
                data = resp.json()
                elements = data.get('elements', [])
                
                if elements:
                    source = {
                        'name': 'OpenStreetMap Telecom Infrastructure',
                        'url': 'https://www.openstreetmap.org/',
                        'type': 'osm',
                        'record_count': len(elements),
                        'description': f'Found {len(elements)} telecom infrastructure elements'
                    }
                    results['new_sources'].append(source)
                    
        except Exception as e:
            logger.debug(f"OSM fiber search error: {e}")
            
        return results
        
    def _search_provider_sites(self, results: Dict) -> Dict:
        """Search telecom provider websites for network maps/KMZ files"""
        provider_patterns = [
            {'provider': 'Zayo', 'search': 'site:zayo.com network map kmz', 'base': 'https://zayo.com'},
            {'provider': 'Lumen', 'search': 'site:lumen.com fiber network map', 'base': 'https://www.lumen.com'},
            {'provider': 'Crown Castle', 'search': 'site:crowncastle.com fiber map', 'base': 'https://www.crowncastle.com'},
            {'provider': 'Cogent', 'search': 'site:cogentco.com network map', 'base': 'https://www.cogentco.com'},
        ]
        
        for provider in provider_patterns:
            try:
                network_pages = [
                    f"{provider['base']}/network",
                    f"{provider['base']}/network-map",
                    f"{provider['base']}/our-network",
                    f"{provider['base']}/infrastructure",
                ]
                
                for page_url in network_pages:
                    try:
                        resp = self.session.get(page_url, timeout=10)
                        results['sources_checked'] += 1
                        
                        if resp.status_code == 200:
                            soup = BeautifulSoup(resp.text, 'html.parser')
                            
                            for link in soup.find_all('a', href=True):
                                href = link['href'].lower()
                                if any(ext in href for ext in ['.kmz', '.kml', '.geojson']):
                                    full_url = urljoin(page_url, link['href'])
                                    source = {
                                        'name': f"{provider['provider']} Network File",
                                        'url': full_url,
                                        'type': 'provider_site',
                                        'provider': provider['provider'],
                                        'file_type': 'kmz' if '.kmz' in href else 'kml' if '.kml' in href else 'geojson'
                                    }
                                    results['new_sources'].append(source)
                                    self._save_fiber_source(source)
                                    
                    except:
                        pass
                        
                time.sleep(0.5)
            except Exception as e:
                logger.debug(f"Provider search error for {provider['provider']}: {e}")
                
        return results
        
    def _save_fiber_source(self, source: Dict):
        """Save discovered fiber source to database"""
        try:
            conn = get_db(self.db_path)
            c = conn.cursor()
            
            c.execute('''INSERT INTO discovered_fiber_sources 
                         (source_name, source_url, source_type, file_type, provider, last_checked)
                         VALUES (%s, %s, %s, %s, %s, %s)
                             ON CONFLICT DO NOTHING''',

                      (source.get('name', ''), source.get('url', ''), 
                       source.get('type', ''), source.get('file_type', ''),
                       source.get('provider', ''), datetime.now().isoformat()))
            
            conn.commit()
            conn.close()
        except Exception as e:
            logger.debug(f"Error saving fiber source: {e}")
            
    def run_utility_discovery(self) -> Dict:
        """Proactively search for utility/power infrastructure data"""
        logger.info("Starting proactive utility discovery...")
        
        conn = get_db(self.db_path)
        c = conn.cursor()
        
        c.execute('''INSERT INTO discovery_runs (run_type, started_at, status) 
                     VALUES ('utility', %s, 'running') ON CONFLICT (run_type) DO UPDATE SET started_at = EXCLUDED.started_at, status = EXCLUDED.status''', (datetime.now().isoformat(),))
        run_id = c.lastrowid
        conn.commit()
        
        results = {
            'sources_checked': 0,
            'new_sources': [],
            'substations_found': 0,
            'power_plants_found': 0,
            'grid_data_found': 0
        }
        
        results = self._search_eia_data(results)
        results = self._search_hifld_data(results)
        results = self._search_osm_power(results)
        results = self._search_state_puc(results)
        
        c.execute('''UPDATE discovery_runs SET 
                     completed_at = %s, sources_checked = %s, new_sources_found = %s, status = 'completed'
                     WHERE id = %s''', 
                  (datetime.now().isoformat(), results['sources_checked'], 
                   len(results['new_sources']), run_id))
        conn.commit()
        conn.close()
        
        logger.info(f"Utility discovery complete: {len(results['new_sources'])} new sources found")
        return results
        
    def _search_eia_data(self, results: Dict) -> Dict:
        """Search EIA for power plant and utility data"""
        try:
            eia_datasets = [
                {'name': 'EIA-860 Power Plants', 'url': 'https://www.eia.gov/electricity/data/eia860/', 'type': 'power_plants'},
                {'name': 'EIA-861 Utility Data', 'url': 'https://www.eia.gov/electricity/data/eia861/', 'type': 'utilities'},
                {'name': 'EIA Electricity Grid', 'url': 'https://www.eia.gov/electricity/gridmonitor/', 'type': 'grid'},
            ]
            
            for dataset in eia_datasets:
                try:
                    resp = self.session.get(dataset['url'], timeout=15)
                    results['sources_checked'] += 1
                    
                    if resp.status_code == 200:
                        soup = BeautifulSoup(resp.text, 'html.parser')
                        
                        download_links = []
                        for link in soup.find_all('a', href=True):
                            href = link['href'].lower()
                            if any(ext in href for ext in ['.xlsx', '.csv', '.zip']):
                                download_links.append(urljoin(dataset['url'], link['href']))
                                
                        if download_links:
                            source = {
                                'name': dataset['name'],
                                'url': dataset['url'],
                                'type': 'eia',
                                'data_type': dataset['type'],
                                'files': download_links[:5]
                            }
                            results['new_sources'].append(source)
                            self._save_utility_source(source)
                            
                except Exception as e:
                    logger.debug(f"EIA search error: {e}")
                    
        except Exception as e:
            logger.error(f"EIA search failed: {e}")
            
        return results
        
    def _search_hifld_data(self, results: Dict) -> Dict:
        """Search HIFLD for infrastructure data"""
        try:
            hifld_datasets = [
                {'name': 'Electric Substations', 'id': 'electric-substations', 'type': 'substations'},
                {'name': 'Electric Power Transmission Lines', 'id': 'electric-power-transmission-lines', 'type': 'transmission'},
                {'name': 'Power Plants', 'id': 'power-plants', 'type': 'power_plants'},
            ]
            
            for dataset in hifld_datasets:
                try:
                    url = f"https://hifld-geoplatform.opendata.arcgis.com/api/v3/datasets/{dataset['id']}"
                    resp = self.session.get(url, timeout=15)
                    results['sources_checked'] += 1
                    
                    if resp.status_code == 200:
                        data = resp.json()
                        attrs = data.get('data', {}).get('attributes', {})
                        
                        source = {
                            'name': dataset['name'],
                            'url': f"https://hifld-geoplatform.opendata.arcgis.com/datasets/{dataset['id']}",
                            'type': 'hifld',
                            'data_type': dataset['type'],
                            'record_count': attrs.get('recordCount', 0),
                            'api_available': True
                        }
                        results['new_sources'].append(source)
                        self._save_utility_source(source)
                        
                        if dataset['type'] == 'substations':
                            results['substations_found'] = attrs.get('recordCount', 0)
                        elif dataset['type'] == 'power_plants':
                            results['power_plants_found'] = attrs.get('recordCount', 0)
                            
                except Exception as e:
                    logger.debug(f"HIFLD search error: {e}")
                    
        except Exception as e:
            logger.error(f"HIFLD search failed: {e}")
            
        return results
        
    def _search_osm_power(self, results: Dict) -> Dict:
        """Search OpenStreetMap for power infrastructure near data center hubs"""
        try:
            dc_hubs = [
                {'name': 'Northern Virginia', 'lat': 38.9, 'lon': -77.4, 'radius': 50000},
                {'name': 'Dallas', 'lat': 32.78, 'lon': -96.8, 'radius': 40000},
                {'name': 'Phoenix', 'lat': 33.45, 'lon': -112.07, 'radius': 40000},
                {'name': 'Atlanta', 'lat': 33.75, 'lon': -84.39, 'radius': 30000},
                {'name': 'Chicago', 'lat': 41.88, 'lon': -87.63, 'radius': 30000},
            ]
            
            overpass_url = "https://overpass-api.de/api/interpreter"
            total_substations = 0
            
            for hub in dc_hubs[:3]:
                try:
                    query = f'''
                    [out:json][timeout:30];
                    (
                      node["power"="substation"](around:{hub['radius']},{hub['lat']},{hub['lon']});
                      way["power"="substation"](around:{hub['radius']},{hub['lat']},{hub['lon']});
                      node["power"="plant"](around:{hub['radius']},{hub['lat']},{hub['lon']});
                    );
                    out center meta;
                    '''
                    
                    resp = self.session.post(overpass_url, data={'data': query}, timeout=45)
                    results['sources_checked'] += 1
                    
                    if resp.status_code == 200:
                        data = resp.json()
                        elements = data.get('elements', [])
                        total_substations += len(elements)
                        
                    time.sleep(2)
                except Exception as e:
                    logger.debug(f"OSM power search error for {hub['name']}: {e}")
                    
            if total_substations > 0:
                source = {
                    'name': 'OpenStreetMap Power Infrastructure',
                    'url': 'https://www.openstreetmap.org/',
                    'type': 'osm',
                    'data_type': 'power',
                    'record_count': total_substations
                }
                results['new_sources'].append(source)
                results['substations_found'] += total_substations
                
        except Exception as e:
            logger.debug(f"OSM power search failed: {e}")
            
        return results
        
    def _search_state_puc(self, results: Dict) -> Dict:
        """Search state Public Utility Commission data"""
        try:
            state_pucs = [
                {'state': 'Texas', 'url': 'https://www.puc.texas.gov/industry/electric/maps/', 'name': 'Texas PUC'},
                {'state': 'California', 'url': 'https://www.cpuc.ca.gov/industries-and-topics/electrical-energy', 'name': 'California PUC'},
                {'state': 'Virginia', 'url': 'https://scc.virginia.gov/pages/Electric', 'name': 'Virginia SCC'},
            ]
            
            for puc in state_pucs:
                try:
                    resp = self.session.get(puc['url'], timeout=15)
                    results['sources_checked'] += 1
                    
                    if resp.status_code == 200:
                        source = {
                            'name': puc['name'],
                            'url': puc['url'],
                            'type': 'state_puc',
                            'data_type': 'regulatory',
                            'coverage_area': puc['state']
                        }
                        results['new_sources'].append(source)
                        self._save_utility_source(source)
                        
                except Exception as e:
                    logger.debug(f"State PUC search error: {e}")
                    
        except Exception as e:
            logger.error(f"State PUC search failed: {e}")
            
        return results
        
    def _save_utility_source(self, source: Dict):
        """Save discovered utility source to database"""
        try:
            conn = get_db(self.db_path)
            c = conn.cursor()
            
            c.execute('''INSERT INTO discovered_utility_sources 
                         (source_name, source_url, data_type, coverage_area, record_count, api_available)
                         VALUES (%s, %s, %s, %s, %s, %s)
                             ON CONFLICT DO NOTHING''',

                      (source.get('name', ''), source.get('url', ''), 
                       source.get('data_type', ''), source.get('coverage_area', ''),
                       source.get('record_count', 0), 1 if source.get('api_available') else 0))
            
            conn.commit()
            conn.close()
        except Exception as e:
            logger.debug(f"Error saving utility source: {e}")
            
    def download_discovered_files(self, limit: int = 5) -> Dict:
        """Download discovered KMZ/KML files"""
        conn = get_db(self.db_path)
        c = conn.cursor()
        
        c.execute('''SELECT id, source_url, source_name, file_type, provider 
                     FROM discovered_fiber_sources 
                     WHERE status = 'pending' AND source_url LIKE '%.km%'
                     LIMIT %s''', (limit,))
        
        sources = c.fetchall()
        results = {'downloaded': [], 'failed': []}
        
        upload_dir = os.path.join(os.getcwd(), 'uploads', 'kmz')
        os.makedirs(upload_dir, exist_ok=True)
        
        for source in sources:
            source_id, url, name, file_type, provider = source
            try:
                resp = self.session.get(url, timeout=30)
                if resp.status_code == 200:
                    filename = os.path.basename(urlparse(url).path)
                    if not filename.endswith(('.kmz', '.kml')):
                        filename = f"{hashlib.md5(url.encode()).hexdigest()[:8]}.kmz"
                        
                    filepath = os.path.join(upload_dir, filename)
                    
                    with open(filepath, 'wb') as f:
                        f.write(resp.content)
                        
                    c.execute('''INSERT INTO downloaded_files 
                                 (source_id, file_url, file_name, file_type, file_size, local_path)
                                 VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (source_id) DO UPDATE SET file_url = EXCLUDED.file_url, file_name = EXCLUDED.file_name, file_type = EXCLUDED.file_type, file_size = EXCLUDED.file_size, local_path = EXCLUDED.local_path''',
                              (source_id, url, filename, file_type, len(resp.content), filepath))
                    
                    c.execute('''UPDATE discovered_fiber_sources SET status = 'downloaded' WHERE id = %s''', 
                              (source_id,))
                    
                    results['downloaded'].append({'name': name, 'file': filename, 'size': len(resp.content)})
                else:
                    c.execute('''UPDATE discovered_fiber_sources SET status = 'failed' WHERE id = %s''', 
                              (source_id,))
                    results['failed'].append({'name': name, 'error': f'HTTP {resp.status_code}'})
                    
            except Exception as e:
                c.execute('''UPDATE discovered_fiber_sources SET status = 'failed' WHERE id = %s''', 
                          (source_id,))
                results['failed'].append({'name': name, 'error': str(e)})
                
        conn.commit()
        conn.close()
        return results
        
    def get_discovery_status(self) -> Dict:
        """Get current discovery status"""
        conn = get_db(self.db_path)
        c = conn.cursor()
        
        try:
            c.execute('SELECT COUNT(*) FROM discovered_fiber_sources')
            fiber_sources = c.fetchone()[0]
        except:
            fiber_sources = 0
        
        try:
            c.execute('SELECT COUNT(*) FROM discovered_utility_sources')
            utility_sources = c.fetchone()[0]
        except:
            utility_sources = 0
        
        try:
            c.execute('SELECT COUNT(*) FROM downloaded_files WHERE processed = 0')
            pending_files = c.fetchone()[0]
        except:
            pending_files = 0
        
        try:
            c.execute('SELECT COUNT(*) FROM downloaded_files WHERE processed = 1')
            processed_files = c.fetchone()[0]
        except:
            processed_files = 0
        
        try:
            c.execute('''SELECT run_type, started_at, status FROM discovery_runs ORDER BY id DESC LIMIT 5''')
            recent_runs = [{'type': r[0], 'started': r[1], 'status': r[2], 'found': 0} 
                           for r in c.fetchall()]
        except:
            recent_runs = []
        
        try:
            c.execute('''SELECT source_name, source_type, discovered_at 
                         FROM discovered_fiber_sources 
                         ORDER BY discovered_at DESC LIMIT 10''')
            recent_fiber = [{'name': r[0], 'type': r[1], 'discovered': r[2]} for r in c.fetchall()]
        except:
            recent_fiber = []
        
        try:
            c.execute('''SELECT source_name, data_type, discovered_at 
                         FROM discovered_utility_sources 
                         ORDER BY discovered_at DESC LIMIT 10''')
            recent_utility = [{'name': r[0], 'type': r[1], 'discovered': r[2]} for r in c.fetchall()]
        except:
            recent_utility = []
        
        conn.close()
        
        return {
            'fiber_sources': fiber_sources,
            'utility_sources': utility_sources,
            'pending_files': pending_files,
            'processed_files': processed_files,
            'recent_runs': recent_runs,
            'recent_fiber_sources': recent_fiber,
            'recent_utility_sources': recent_utility
        }
        
    def run_full_discovery(self) -> Dict:
        """Run complete discovery cycle for fiber and utility sources"""
        logger.info("Starting full proactive discovery cycle...")
        
        fiber_results = self.run_fiber_discovery()
        utility_results = self.run_utility_discovery()
        download_results = self.download_discovered_files()
        
        return {
            'fiber': fiber_results,
            'utility': utility_results,
            'downloads': download_results,
            'status': self.get_discovery_status()
        }


def create_proactive_discovery_blueprint(db_path: str = 'dc_nexus.db'):
    """Create Flask blueprint for proactive discovery endpoints"""
    bp = Blueprint('proactive_discovery', __name__)
    engine = ProactiveDiscoveryEngine(db_path)
    
    @bp.route('/api/discovery/proactive/status')
    def discovery_status():
        return jsonify({"success": True, "data": engine.get_discovery_status()})
        
    @bp.route('/api/discovery/proactive/fiber', methods=['POST'])
    def discover_fiber():
        def run_async():
            return engine.run_fiber_discovery()
        results = run_async()
        return jsonify({"success": True, "data": results})
        
    @bp.route('/api/discovery/proactive/utility', methods=['POST'])
    def discover_utility():
        def run_async():
            return engine.run_utility_discovery()
        results = run_async()
        return jsonify({"success": True, "data": results})
        
    @bp.route('/api/discovery/proactive/download', methods=['POST'])
    def download_files():
        data = request.get_json() or {}
        limit = data.get('limit', 5)
        results = engine.download_discovered_files(limit)
        return jsonify({"success": True, "data": results})
        
    @bp.route('/api/discovery/proactive/full', methods=['POST'])
    def full_discovery():
        def run_async():
            return engine.run_full_discovery()
        results = run_async()
        return jsonify({"success": True, "data": results})
        
    @bp.route('/api/discovery/proactive/sources/fiber')
    def list_fiber_sources():
        conn = get_db(db_path)
        c = conn.cursor()
        c.execute('''SELECT source_name, source_url, source_type, file_type, provider, status, discovered_at
                     FROM discovered_fiber_sources ORDER BY discovered_at DESC LIMIT 50''')
        sources = [{'name': r[0], 'url': r[1], 'type': r[2], 'file_type': r[3], 
                   'provider': r[4], 'status': r[5], 'discovered': r[6]} for r in c.fetchall()]
        conn.close()
        return jsonify({"success": True, "data": sources})
        
    @bp.route('/api/discovery/proactive/sources/utility')
    def list_utility_sources():
        conn = get_db(db_path)
        c = conn.cursor()
        c.execute('''SELECT source_name, source_url, data_type, coverage_area, record_count, api_available
                     FROM discovered_utility_sources ORDER BY discovered_at DESC LIMIT 50''')
        sources = [{'name': r[0], 'url': r[1], 'data_type': r[2], 'coverage': r[3],
                   'records': r[4], 'has_api': bool(r[5])} for r in c.fetchall()]
        conn.close()
        return jsonify({"success": True, "data": sources})
        
    return bp, engine
