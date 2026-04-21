"""
DC Hub KMZ/KML Processor — Wave 1B
Processes registered KMZ/KML sources into Neon infrastructure_layers table.
"""
import os, io, json, zipfile, logging, sqlite3, traceback
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
import requests
from internal_auth import is_valid_internal_key, get_internal_key_for_client

logger = logging.getLogger(__name__)
SQLITE_DB = os.environ.get('SQLITE_DB', 'dc_nexus.db')
DOWNLOAD_TIMEOUT = 60
MAX_FEATURES_PER_SOURCE = 5000
BATCH_SIZE = 50
PRIORITY_CATEGORIES = ['fiber', 'power', 'transmission', 'substation']


class KMZParser:
    def __init__(self):
        self._kml_available = False
        self._shapely_available = False
        try:
            from fastkml import kml as fk
            self._fastkml = fk
            self._kml_available = True
        except ImportError:
            logger.warning("fastkml not available — using xml.etree fallback")
        try:
            from shapely.geometry import mapping as sm
            self._shapely_mapping = sm
            self._shapely_available = True
        except ImportError:
            pass

    def parse_file(self, content, source_url, source_id):
        # Try GeoJSON first (from ArcGIS queries)
        try:
            data = json.loads(content)
            if 'features' in data and isinstance(data['features'], list):
                return self._parse_geojson(data, source_url, source_id)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
        
        # Fall back to KML/KMZ
        kml_content = self._extract_kml(content)
        if kml_content is None:
            return []
        if self._kml_available:
            return self._parse_with_fastkml(kml_content, source_url, source_id)
        return self._parse_with_xml(kml_content, source_url, source_id)

    def _parse_geojson(self, data, source_url, source_id):
        """Parse GeoJSON FeatureCollection from ArcGIS."""
        features = []
        for feat in data.get('features', [])[:MAX_FEATURES_PER_SOURCE]:
            try:
                geom = feat.get('geometry', {})
                props = feat.get('properties', {})
                geom_type = geom.get('type', 'Unknown')
                coords = {'type': geom_type, 'coordinates': geom.get('coordinates', [])}
                name = str(props.get('NAME', props.get('name', props.get('OBJECTID', ''))))[:500]
                desc = str(props.get('DESCRIPTION', props.get('description', '')))[:2000]
                category = self._infer_category(name, '', desc, props)
                features.append({
                    'source_id': source_id, 'source_url': source_url,
                    'geometry_type': geom_type, 'coordinates': coords,
                    'name': name, 'description': desc, 'layer_name': '',
                    'attributes': {k: str(v)[:500] for k, v in list(props.items())[:20]},
                    'category': category,
                })
            except Exception:
                continue
        logger.info(f"GeoJSON parsed: {len(features)} features from {source_url}")
        return features

    def _extract_kml(self, content):
        if content[:4] == b'PK\x03\x04':
            try:
                with zipfile.ZipFile(io.BytesIO(content)) as zf:
                    kml_files = [f for f in zf.namelist() if f.lower().endswith('.kml')]
                    if kml_files:
                        return zf.read(kml_files[0])
                    if 'doc.kml' in zf.namelist():
                        return zf.read('doc.kml')
                    return None
            except zipfile.BadZipFile:
                return None
        if b'<kml' in content[:1000] or b'<Document' in content[:1000]:
            return content
        return None

    def _parse_with_fastkml(self, kml_bytes, source_url, source_id):
        from fastkml import kml as fkml
        features = []
        try:
            k = fkml.KML()
            k.from_string(kml_bytes)
            def extract(element, layer=""):
                try:
                    for feat in getattr(element, 'features', lambda: [])():
                        ft = type(feat).__name__
                        if ft in ('Folder', 'Document'):
                            extract(feat, getattr(feat, 'name', '') or layer)
                        elif ft == 'Placemark':
                            p = self._parse_placemark(feat, source_url, source_id, layer)
                            if p:
                                features.append(p)
                                if len(features) >= MAX_FEATURES_PER_SOURCE:
                                    return
                except Exception as e:
                    logger.debug(f"Extract error: {e}")
            extract(k)
        except Exception as e:
            logger.error(f"fastkml parse error: {e}")
        return features

    def _parse_placemark(self, pm, source_url, source_id, layer_name):
        try:
            geom = getattr(pm, 'geometry', None)
            if geom is None:
                return None
            geom_type = geom.geom_type if hasattr(geom, 'geom_type') else type(geom).__name__
            coords = self._shapely_mapping(geom) if self._shapely_available else self._extract_coords_raw(geom)
            name = getattr(pm, 'name', '') or ''
            desc = getattr(pm, 'description', '') or ''
            attrs = {}
            ext = getattr(pm, 'extended_data', None)
            if ext:
                for de in getattr(ext, 'elements', []):
                    if hasattr(de, 'data'):
                        for d in de.data:
                            if hasattr(d, 'name') and hasattr(d, 'value'):
                                attrs[d.name] = d.value
            return {
                'source_id': source_id, 'source_url': source_url,
                'geometry_type': geom_type,
                'coordinates': coords if isinstance(coords, dict) else {'raw': str(coords)[:10000]},
                'name': name[:500], 'description': desc[:2000],
                'layer_name': layer_name[:200], 'attributes': attrs,
                'category': self._infer_category(name, layer_name, desc, attrs),
            }
        except Exception:
            return None

    def _extract_coords_raw(self, geom):
        try:
            if hasattr(geom, 'coords'):
                return {'type': type(geom).__name__, 'coordinates': list(geom.coords)}
            return {'type': 'Unknown', 'coordinates': []}
        except:
            return {'type': 'Unknown', 'coordinates': []}

    def _parse_with_xml(self, kml_bytes, source_url, source_id):
        import xml.etree.ElementTree as ET
        features = []
        try:
            kml_str = kml_bytes.decode('utf-8', errors='replace').replace('xmlns=', 'xmlns_disabled=')
            root = ET.fromstring(kml_str)
            for pm in root.iter('Placemark'):
                ne = pm.find('name')
                de = pm.find('description')
                name = ne.text if ne is not None and ne.text else ''
                desc = de.text if de is not None and de.text else ''
                for gt in ['Point', 'LineString', 'Polygon', 'MultiGeometry']:
                    ge = pm.find(f'.//{gt}')
                    if ge is not None:
                        ce = ge.find('.//coordinates')
                        if ce is not None and ce.text:
                            coords = self._parse_coord_str(ce.text.strip(), gt)
                            features.append({
                                'source_id': source_id, 'source_url': source_url,
                                'geometry_type': gt, 'coordinates': coords,
                                'name': name[:500], 'description': desc[:2000],
                                'layer_name': '', 'attributes': {},
                                'category': self._infer_category(name, '', desc, {}),
                            })
                            if len(features) >= MAX_FEATURES_PER_SOURCE:
                                return features
                        break
        except Exception as e:
            logger.error(f"XML parse error: {e}")
        return features

    def _parse_coord_str(self, text, geom_type):
        pts = []
        for c in text.split():
            p = c.strip().split(',')
            if len(p) >= 2:
                pts.append([float(p[0]), float(p[1])])
        if geom_type == 'Point' and pts:
            return {'type': 'Point', 'coordinates': pts[0]}
        elif geom_type == 'LineString':
            return {'type': 'LineString', 'coordinates': pts}
        elif geom_type == 'Polygon':
            return {'type': 'Polygon', 'coordinates': [pts]}
        return {'type': geom_type, 'coordinates': pts}

    def _infer_category(self, name, layer, desc, attrs):
        t = f"{name} {layer} {desc} {' '.join(str(v) for v in attrs.values())}".lower()
        if any(k in t for k in ['fiber', 'cable', 'strand', 'dark fiber', 'conduit']):
            return 'fiber'
        if any(k in t for k in ['substation', 'transformer', 'switchyard']):
            return 'substation'
        if any(k in t for k in ['transmission', 'voltage', 'kv ', '345kv', '500kv', '230kv']):
            return 'transmission'
        if any(k in t for k in ['pipeline', 'gas', 'natural gas', 'lng']):
            return 'gas_pipeline'
        if any(k in t for k in ['solar', 'wind', 'generation', 'power plant']):
            return 'power_generation'
        return 'infrastructure'


class InfrastructureLayerDB:
    def __init__(self, neon_conn_func=None):
        self._get_conn = neon_conn_func
        self._table_ensured = False

    def _ensure_table(self):
        """Ensure table exists — called lazily on first write, not at init.
        v2.6: Moved out of __init__ to prevent holding a pool connection
        for the lifetime of the KMZProcessor object (was leaking for 69s+).
        """
        if self._table_ensured:
            return
        sql = """
        CREATE TABLE IF NOT EXISTS infrastructure_layers (
            id SERIAL PRIMARY KEY, source_id VARCHAR(200), source_url TEXT,
            geometry_type VARCHAR(50), coordinates JSONB, name VARCHAR(500),
            description TEXT, layer_name VARCHAR(200),
            attributes JSONB DEFAULT '{}', category VARCHAR(50) DEFAULT 'infrastructure',
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(source_id, name, geometry_type)
        );
        CREATE INDEX IF NOT EXISTS idx_infra_cat ON infrastructure_layers(category);
        CREATE INDEX IF NOT EXISTS idx_infra_src ON infrastructure_layers(source_id);
        """
        conn = None
        try:
            conn = self._get_conn()
            if conn:
                cur = conn.cursor()
                cur.execute(sql)
                conn.commit()
                cur.close()
                self._table_ensured = True
                logger.info("infrastructure_layers table ensured")
        except Exception as e:
            logger.error(f"Table create error: {e}")
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def get_pending_sources(self, limit=10):
        try:
            conn = sqlite3.connect(SQLITE_DB)
            # sqlite3.Row removed - PostgreSQL uses RealDictCursor or dict(row)
            cur = conn.cursor()
            cur.execute("""
                SELECT id, url, category, name, status FROM kmz_discovered_sources
                WHERE status = 'empty'
                ORDER BY CASE WHEN category IN ('fiber','power','transmission','substation') THEN 0 ELSE 1 END, id
                LIMIT %s
            """, (limit,))
            rows = [dict(r) for r in cur.fetchall()]
            conn.close()
            return rows
        except Exception as e:
            logger.error(f"SQLite read error: {e}")
            return []

    def update_source_status(self, source_id, status, features_count=0):
        try:
            conn = sqlite3.connect(SQLITE_DB)
            cur.execute("UPDATE kmz_discovered_sources SET status=%s WHERE id=%s", (status, source_id))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Status update error: {e}")

    def insert_features(self, features):
        if not features:
            return 0
        self._ensure_table()  # Lazy init — only on first actual write
        inserted = 0
        conn = None
        try:
            conn = self._get_conn()
            if not conn:
                return 0
            cur = conn.cursor()
            for feat in features:
                try:
                    cur.execute("""
                        INSERT INTO infrastructure_layers
                            (source_id, source_url, geometry_type, coordinates, name, description, layer_name, attributes, category)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (source_id, name, geometry_type) DO NOTHING
                    """, (feat['source_id'], feat['source_url'], feat['geometry_type'],
                          json.dumps(feat['coordinates']), feat['name'], feat.get('description',''),
                          feat.get('layer_name',''), json.dumps(feat.get('attributes',{})),
                          feat.get('category','infrastructure')))
                    inserted += 1
                except Exception as e:
                    conn.rollback()
                    continue
            conn.commit()
            cur.close()
        except Exception as e:
            logger.error(f"Insert error: {e}")
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
        return inserted


class KMZDownloader:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers['User-Agent'] = 'DC-Hub/1.0 Infrastructure Intelligence'

    def download(self, url):
        try:
            if 'github.com' in url and '/blob/' in url:
                url = url.replace('github.com', 'raw.githubusercontent.com').replace('/blob/', '/')
            
            # ArcGIS FeatureServer — query as GeoJSON instead of KMZ
            if 'arcgis.com' in url and 'FeatureServer' in url:
                return self._download_arcgis(url)
            
            resp = self.session.get(url, timeout=DOWNLOAD_TIMEOUT, allow_redirects=True)
            resp.raise_for_status()
            if len(resp.content) < 100:
                return None
            logger.info(f"Downloaded {len(resp.content):,} bytes from {url}")
            return resp.content
        except Exception as e:
            logger.warning(f"Download error {url}: {e}")
            return None

    def _download_arcgis(self, base_url):
        """Query ArcGIS FeatureServer and return GeoJSON as bytes."""
        try:
            # Ensure we hit the layer endpoint (append /0 if no layer specified)
            query_url = base_url.rstrip('/')
            if query_url.endswith('FeatureServer'):
                query_url += '/0'
            query_url += '/query'
            
            params = {
                'where': '1=1',
                'outFields': '*',
                'f': 'geojson',
                'resultRecordCount': 5000,
            }
            resp = self.session.get(query_url, params=params, timeout=DOWNLOAD_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            if 'features' in data and len(data['features']) > 0:
                logger.info(f"ArcGIS: {len(data['features'])} features from {base_url}")
                return resp.content  # Return raw GeoJSON bytes
            else:
                logger.warning(f"ArcGIS: no features from {base_url}")
                return None
        except Exception as e:
            logger.warning(f"ArcGIS query error {base_url}: {e}")
            return None


class KMZProcessor:
    def __init__(self, neon_conn_func=None):
        self.parser = KMZParser()
        self.downloader = KMZDownloader()
        self.db = InfrastructureLayerDB(neon_conn_func)

    def process_batch(self, batch_size=5):
        results = {'sources_attempted': 0, 'sources_succeeded': 0, 'sources_failed': 0,
                    'features_parsed': 0, 'features_inserted': 0, 'errors': [],
                    'timestamp': datetime.now(timezone.utc).isoformat()}
        sources = self.db.get_pending_sources(limit=batch_size)
        if not sources:
            results['message'] = 'No pending sources'
            return results
        for src in sources:
            results['sources_attempted'] += 1
            sid, url = src['id'], src['url']
            sname = src.get('name', f'source_{sid}')
            try:
                content = self.downloader.download(url)
                if not content:
                    self.db.update_source_status(sid, 'download_failed')
                    results['sources_failed'] += 1
                    results['errors'].append(f"{sname}: download failed")
                    continue
                features = self.parser.parse_file(content, url, str(sid))
                results['features_parsed'] += len(features)
                if not features:
                    self.db.update_source_status(sid, 'parse_failed')
                    results['sources_failed'] += 1
                    results['errors'].append(f"{sname}: no features parsed")
                    continue
                ins = self.db.insert_features(features)
                results['features_inserted'] += ins
                self.db.update_source_status(sid, 'processed', ins)
                results['sources_succeeded'] += 1
                logger.info(f"Processed {sname}: {len(features)} parsed, {ins} inserted")
            except Exception as e:
                logger.error(f"Error processing {sname}: {e}")
                self.db.update_source_status(sid, 'error')
                results['sources_failed'] += 1
                results['errors'].append(f"{sname}: {str(e)[:200]}")
        return results

    def get_stats(self):
        stats = {'sqlite': {}, 'neon': {}}
        try:
            c = sqlite3.connect(SQLITE_DB)
            cur = c.cursor()
            cur.execute("SELECT status, COUNT(*) FROM kmz_discovered_sources GROUP BY status")
            stats['sqlite'] = {r[0]: r[1] for r in cur.fetchall()}
            cur.execute("SELECT COUNT(*) FROM kmz_discovered_sources")
            stats['sqlite']['total'] = cur.fetchone()[0]
            c.close()
        except Exception as e:
            stats['sqlite']['error'] = str(e)
        try:
            nc = self.db._get_conn()
            if nc:
                cur = nc.cursor()
                cur.execute("SELECT category, COUNT(*) FROM infrastructure_layers GROUP BY category")
                stats['neon'] = {r[0]: r[1] for r in cur.fetchall()}
                cur.execute("SELECT COUNT(*) FROM infrastructure_layers")
                stats['neon']['total'] = cur.fetchone()[0]
                cur.close()
                nc.close()
        except Exception as e:
            stats['neon']['error'] = str(e)
        return stats


def register_kmz_routes(app, get_pg_connection):
    from flask import jsonify, request as freq
    processor = KMZProcessor(neon_conn_func=get_pg_connection)

    @app.route('/api/admin/kmz/process', methods=['POST'])
    def kmz_process_batch():
        ik = freq.headers.get('X-Internal-Key')
        if ik != get_internal_key_for_client():
            return jsonify({'error': 'Unauthorized'}), 403
        bs = min(freq.args.get('batch_size', 5, type=int), 20)
        return jsonify({'success': True, **processor.process_batch(batch_size=bs)})

    @app.route('/api/admin/kmz/stats', methods=['GET'])
    def kmz_stats():
        ik = freq.headers.get('X-Internal-Key')
        if ik != get_internal_key_for_client():
            return jsonify({'error': 'Unauthorized'}), 403
        return jsonify({'success': True, **processor.get_stats()})

    @app.route('/api/v1/infrastructure-layers', methods=['GET'])
    def get_infrastructure_layers():
        try:
            cat = freq.args.get('category', '')
            lim = min(freq.args.get('limit', 100, type=int), 500)
            conn = get_pg_connection()
            if not conn:
                return jsonify({'error': 'Database unavailable'}), 503
            cur = conn.cursor()
            q = "SELECT id, source_id, geometry_type, coordinates, name, layer_name, category, attributes FROM infrastructure_layers"
            conds, params = [], []
            if cat:
                conds.append("category = %s")
                params.append(cat)
            if conds:
                q += " WHERE " + " AND ".join(conds)
            q += " ORDER BY id LIMIT %s"
            params.append(lim)
            cur.execute(q, params)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            for r in rows:
                for f in ('coordinates', 'attributes'):
                    if isinstance(r.get(f), str):
                        try: r[f] = json.loads(r[f])
                        except: pass
            cur.close()
            conn.close()
            return jsonify({'success': True, 'count': len(rows), 'data': rows})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    logger.info("KMZ routes: /api/admin/kmz/process, /api/admin/kmz/stats, /api/v1/infrastructure-layers")
    return processor
