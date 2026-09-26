"""
gas_infra_loader.py — populate gas_compressor_stations + gas_processing_plants
from live HIFLD/EIA ArcGIS feature services into Neon.

Created 2026-06-12. The /api/jobs/gas-refresh job (routes/jobs_routes.py) imports
`gas_compressor_loader.load_gas_compressors` and
`gas_processing_loader.load_gas_processings` — thin shims that call into here.

The dchub_cors_patch.py serving routes read columns:
    name, latitude, longitude (or ST_Y/ST_X(geom)), capacity_mmcfd/horsepower,
    operator, state, status
so this loader writes EXACTLY those (plus the canonical id/lat/lng columns and a
PostGIS geom), keeping the tables query-compatible across refreshes.

Sources (re-hosted HIFLD/EIA, anonymous, point geometry, national):
    processing : services.arcgis.com/jDGuO8tYggdCCnUJ/.../Natural_Gas_Processing_Plants
    compressors: services5.arcgis.com/HDRa0B57OVrv2E1q/.../Natural_Gas_Compressor_Stations
"""
import os
import json
import time
import logging
import urllib.request
import urllib.parse
import psycopg2

log = logging.getLogger(__name__)

_UA = {'User-Agent': 'dchub-gas-infra-loader/1.0'}

PROCESSING_URL = ("https://services.arcgis.com/jDGuO8tYggdCCnUJ/arcgis/rest/services/"
                  "Natural_Gas_Processing_Plants/FeatureServer/0")
COMPRESSOR_URL = ("https://services5.arcgis.com/HDRa0B57OVrv2E1q/arcgis/rest/services/"
                  "Natural_Gas_Compressor_Stations/FeatureServer/0")


def _fetch_all(base):
    """Page through an ArcGIS FeatureServer layer -> list of (attributes, geometry)."""
    out, offset = [], 0
    while True:
        q = urllib.parse.urlencode({
            'where': '1=1', 'outFields': '*', 'returnGeometry': 'true',
            'outSR': '4326', 'f': 'json', 'resultOffset': offset, 'resultRecordCount': 1000,
        })
        req = urllib.request.Request(base + '/query?' + q, headers=_UA)
        d = json.loads(urllib.request.urlopen(req, timeout=60).read())
        feats = d.get('features', [])
        for f in feats:
            out.append((f.get('attributes', {}) or {}, f.get('geometry') or {}))
        if len(feats) < 1000:
            break
        offset += len(feats)
        time.sleep(0.2)
    return out


def _conn():
    return psycopg2.connect(os.environ['DATABASE_URL'])


def _has_postgis(cur):
    try:
        cur.execute("SELECT ST_Y(ST_SetSRID(ST_MakePoint(0,0),4326))")
        cur.fetchone()
        return True
    except Exception:
        cur.connection.rollback()
        return False


def load_gas_processings():
    """Refresh gas_processing_plants (478±). Returns row count."""
    rows = _fetch_all(PROCESSING_URL)
    conn = _conn()
    conn.autocommit = False
    cur = conn.cursor()
    pg = _has_postgis(cur)
    cur.execute("""
        ALTER TABLE gas_processing_plants
          ADD COLUMN IF NOT EXISTS name text,
          ADD COLUMN IF NOT EXISTS latitude double precision,
          ADD COLUMN IF NOT EXISTS longitude double precision
    """)
    if pg:
        cur.execute("ALTER TABLE gas_processing_plants ADD COLUMN IF NOT EXISTS geom geometry")
    cur.execute("TRUNCATE gas_processing_plants RESTART IDENTITY")
    n = 0
    for a, g in rows:
        lat, lng = g.get('y'), g.get('x')
        if lat is None or lng is None:
            continue
        cur.execute(
            """INSERT INTO gas_processing_plants
               (eia_id, plant_name, name, operator, state, county, capacity_mmcfd,
                status, lat, lng, latitude, longitude, source, loaded_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now() ON CONFLICT DO NOTHING)""",
            (str(a.get('FID') or a.get('OBJECTID') or ''), a.get('Plant_Name'), a.get('Plant_Name'),
             a.get('Operator') or a.get('Owner'), a.get('State'), a.get('County'),
             a.get('Cap_MMcfd'), a.get('Status') or 'Operating', lat, lng, lat, lng,
             'HIFLD/EIA via ArcGIS jDGuO8tYggdCCnUJ'))
        n += 1
    if pg:
        cur.execute("UPDATE gas_processing_plants SET geom=ST_SetSRID(ST_MakePoint(lng,lat),4326) WHERE lat IS NOT NULL")
    conn.commit()
    conn.close()
    log.info("gas_infra_loader: %d processing plants", n)
    return n


def load_gas_compressors():
    """Refresh gas_compressor_stations (1,768±). Returns row count."""
    rows = _fetch_all(COMPRESSOR_URL)
    conn = _conn()
    conn.autocommit = False
    cur = conn.cursor()
    pg = _has_postgis(cur)
    cur.execute("""
        ALTER TABLE gas_compressor_stations
          ADD COLUMN IF NOT EXISTS name text,
          ADD COLUMN IF NOT EXISTS latitude double precision,
          ADD COLUMN IF NOT EXISTS longitude double precision,
          ADD COLUMN IF NOT EXISTS horsepower numeric,
          ADD COLUMN IF NOT EXISTS status text
    """)
    if pg:
        cur.execute("ALTER TABLE gas_compressor_stations ADD COLUMN IF NOT EXISTS geom geometry")
    cur.execute("TRUNCATE gas_compressor_stations RESTART IDENTITY")
    n = 0
    for a, g in rows:
        lat, lng = g.get('y'), g.get('x')
        if lat is None or lng is None:
            continue
        cur.execute(
            """INSERT INTO gas_compressor_stations
               (hifld_id, station_name, name, operator, county, state, status,
                lat, lng, latitude, longitude, source, loaded_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now() ON CONFLICT DO NOTHING)""",
            (str(a.get('GCOMPID') or a.get('OBJECTID') or ''), a.get('NAME'), a.get('NAME'),
             a.get('OPERATOR') or a.get('Operator'), a.get('COUNTY'), a.get('STATE'),
             a.get('STATUS') or 'Operating', lat, lng, lat, lng,
             'HIFLD via ArcGIS HDRa0B57OVrv2E1q'))
        n += 1
    if pg:
        cur.execute("UPDATE gas_compressor_stations SET geom=ST_SetSRID(ST_MakePoint(lng,lat),4326) WHERE lat IS NOT NULL")
    conn.commit()
    conn.close()
    log.info("gas_infra_loader: %d compressor stations", n)
    return n


def refresh_all():
    out = {'processing': load_gas_processings(), 'compressors': load_gas_compressors()}
    # 2026-06-12: report to the autonomous-intelligence extraction ledger
    # (observatory showed "1 sources" because loaders never wrote to it).
    try:
        from routes.extractor_brain import record_extraction
        record_extraction("gas-infra-loader", "success",
                          rows_inserted=int(out.get('processing') or 0) + int(out.get('compressors') or 0),
                          observations=out)
    except Exception:
        pass
    return out


if __name__ == '__main__':
    print(refresh_all())
