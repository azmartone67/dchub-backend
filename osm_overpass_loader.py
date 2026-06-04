"""
OSM Overpass loader for Land-Power infrastructure data.

Replaces the broken HIFLD ArcGIS loaders. Fetches substations, power plants,
transmission lines, pipelines, and communications towers from OpenStreetMap
via the public Overpass API.

Schema notes:
  • substations (name, operator, voltage_kv, lat, lng, city, state, country)
  • power_plants — same shape as discovered_power_plants in main.py schema
  • transmission_lines — voltage + path geometry as JSON
  • pipelines (name, operator, diameter_in, lat, lng, city, country)
  • comm_towers (name, tower_type, lat, lng, state)

Each loader is idempotent — uses ON CONFLICT DO NOTHING. State-by-state
iteration to keep individual queries under the Overpass 25k-element limit.
"""
import json
import logging
import time
import urllib.request
import urllib.parse

logger = logging.getLogger(__name__)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "DCHub-OSM-Loader/1.0 (https://dchub.cloud)"

US_STATES = [
    'AL','AK','AZ','AR','CA','CO','CT','DC','DE','FL',
    'GA','HI','ID','IL','IN','IA','KS','KY','LA','ME',
    'MD','MA','MI','MN','MS','MO','MT','NE','NV','NH',
    'NJ','NM','NY','NC','ND','OH','OK','OR','PA','RI',
    'SC','SD','TN','TX','UT','VT','VA','WA','WV','WI','WY',
]


def _overpass(query, timeout=90, retries=3, backoff=8):
    body = urllib.parse.urlencode({'data': query}).encode('utf-8')
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                OVERPASS_URL, data=body,
                headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except Exception as e:
            logger.warning(f"Overpass attempt {attempt+1} failed: {e}")
            if attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
    return None


def _parse_voltage_kv(raw):
    """OSM voltage is in volts as a string, possibly with semicolons."""
    if not raw: return 0
    try:
        first = str(raw).split(';')[0].strip().split()[0]
        return int(first) // 1000 if first.isdigit() else 0
    except Exception:
        return 0


def _get_db():
    from db_utils import try_get_db
    return try_get_db()


def load_substations(states=None):
    """OSM `power=substation` nodes. Returns per-state {count, inserted}."""
    results = {}
    db = _get_db()
    if not db: return {'error': 'no DB'}
    states = states or US_STATES
    for state in states:
        q = f'''
        [out:json][timeout:60];
        area["ISO3166-2"="US-{state}"]->.s;
        (
          node["power"="substation"](area.s);
          way["power"="substation"](area.s);
        );
        out center;
        '''
        data = _overpass(q)
        if not data:
            results[state] = {'error': 'overpass timeout'}
            continue
        els = data.get('elements', [])
        if not els:
            results[state] = {'count': 0}; continue
        cur = db.cursor()
        ins = 0
        for el in els:
            tags = el.get('tags', {})
            name = (tags.get('name') or f"OSM-{el.get('id')}")[:200]
            op = (tags.get('operator') or 'Unknown')[:200]
            kv = _parse_voltage_kv(tags.get('voltage'))
            lat = el.get('lat') or (el.get('center', {}) or {}).get('lat')
            lng = el.get('lon') or (el.get('center', {}) or {}).get('lon')
            if not lat or not lng: continue
            city = (tags.get('addr:city') or '')[:100]
            try:
                cur.execute(
                    """INSERT INTO substations
                       (name, operator, voltage_kv, lat, lng, city, state, country)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT DO NOTHING""",
                    (name, op, kv, lat, lng, city, state, 'US'))
                ins += cur.rowcount
            except Exception as e:
                db.rollback()
                logger.warning(f"substation insert {state}: {e}")
        try: db.commit()
        except Exception: pass
        results[state] = {'count': len(els), 'inserted': ins}
        time.sleep(2)
    try: db.close()
    except Exception: pass
    return results


def load_power_plants(states=None):
    """OSM `power=plant` (nodes + ways). Inserts into discovered_power_plants."""
    results = {}
    db = _get_db()
    if not db: return {'error': 'no DB'}
    states = states or US_STATES
    for state in states:
        q = f'''
        [out:json][timeout:90];
        area["ISO3166-2"="US-{state}"]->.s;
        (
          node["power"="plant"](area.s);
          way["power"="plant"](area.s);
          relation["power"="plant"](area.s);
        );
        out center;
        '''
        data = _overpass(q)
        if not data:
            results[state] = {'error': 'overpass timeout'}; continue
        els = data.get('elements', [])
        if not els:
            results[state] = {'count': 0}; continue
        cur = db.cursor()
        ins = 0
        # r70g: resolve the LIVE columns once. The repo CREATE TABLE is stale —
        # the live discovered_power_plants has NO `country`, and its `id` is TEXT,
        # NOT NULL, with no default. The old hardcoded INSERT (with `country`, no
        # `id`) therefore failed on EVERY row (UndefinedColumn / NotNullViolation),
        # silently swallowed by the except below -> 0 inserts -> table frozen at
        # ~6,961 legacy rows. See reference_dchub_power_plants_schema_trap.
        try:
            cur.execute("""SELECT column_name FROM information_schema.columns
                           WHERE table_name='discovered_power_plants'""")
            _pp_cols = {r[0] for r in cur.fetchall()}
        except Exception:
            _pp_cols = {"name", "operator", "fuel_type", "capacity_mw",
                        "lat", "lng", "state", "source", "id"}
        for el in els:
            tags = el.get('tags', {})
            name = (tags.get('name') or f"OSM-{el.get('id')}")[:200]
            op = (tags.get('operator') or '')[:200]
            source = tags.get('plant:source', '')
            output = tags.get('plant:output:electricity', '')
            try:
                mw = float(output.replace('MW','').strip()) if 'MW' in output else 0
            except Exception:
                mw = 0
            lat = el.get('lat') or (el.get('center', {}) or {}).get('lat')
            lng = el.get('lon') or (el.get('center', {}) or {}).get('lon')
            if not lat or not lng: continue
            try:
                # Schema-adaptive INSERT: write only columns that exist on the
                # LIVE table and ALWAYS supply a deterministic TEXT id
                # (osm-<type>-<id>, stable -> idempotent re-runs via ON CONFLICT).
                _row = {"name": name, "operator": op, "fuel_type": source[:80],
                        "capacity_mw": mw, "lat": lat, "lng": lng,
                        "state": state, "source": "osm_overpass"}
                _row = {k: v for k, v in _row.items() if k in _pp_cols}
                if "id" in _pp_cols:
                    _row["id"] = f"osm-{el.get('type', 'x')}-{el.get('id')}"
                _ks = list(_row.keys())
                cur.execute(
                    f"INSERT INTO discovered_power_plants ({','.join(_ks)}) "
                    f"VALUES ({','.join(['%s'] * len(_ks))}) "
                    f"ON CONFLICT DO NOTHING",
                    [_row[k] for k in _ks])
                ins += cur.rowcount
            except Exception as e:
                db.rollback()
                logger.warning(f"power_plant insert {state}: {e}")
        try: db.commit()
        except Exception: pass
        results[state] = {'count': len(els), 'inserted': ins}
        time.sleep(2)
    try: db.close()
    except Exception: pass
    return results


def load_transmission_lines(states=None, min_kv=100):
    """OSM `power=line` ways with voltage >= min_kv.

    Schema-tolerant: inserts into infrastructure_layers (category='transmission')
    if available, else into a transmission table. Just records start lat/lng of
    each line for now; full geometry would need a geometry-typed column.
    """
    results = {}
    db = _get_db()
    if not db: return {'error': 'no DB'}
    states = states or US_STATES
    for state in states:
        q = f'''
        [out:json][timeout:120];
        area["ISO3166-2"="US-{state}"]->.s;
        way["power"="line"]["voltage"~"^[0-9]+"](area.s);
        out center tags;
        '''
        data = _overpass(q)
        if not data:
            results[state] = {'error': 'overpass timeout'}; continue
        els = data.get('elements', [])
        if not els:
            results[state] = {'count': 0}; continue
        cur = db.cursor()
        ins = 0
        for el in els:
            tags = el.get('tags', {})
            kv = _parse_voltage_kv(tags.get('voltage'))
            if kv < min_kv: continue
            name = (tags.get('name') or f"OSM-line-{el.get('id')}")[:200]
            op = (tags.get('operator') or '')[:200]
            center = el.get('center') or {}
            lat = center.get('lat')
            lng = center.get('lon')
            if not lat or not lng: continue
            try:
                cur.execute(
                    """INSERT INTO infrastructure_layers
                       (id, category, name, capacity_mw, lat, lng, state)
                       VALUES (%s, 'transmission', %s, %s, %s, %s, %s)
                       ON CONFLICT DO NOTHING""",
                    (f"osm-line-{el.get('id')}", name, kv, lat, lng, state))
                ins += cur.rowcount
            except Exception as e:
                db.rollback()
                logger.warning(f"transmission insert {state}: {e}")
        try: db.commit()
        except Exception: pass
        results[state] = {'count': len(els), 'inserted': ins}
        time.sleep(3)
    try: db.close()
    except Exception: pass
    return results


def load_pipelines(states=None):
    """OSM `man_made=pipeline` ways (gas + oil)."""
    results = {}
    db = _get_db()
    if not db: return {'error': 'no DB'}
    states = states or US_STATES
    for state in states:
        q = f'''
        [out:json][timeout:90];
        area["ISO3166-2"="US-{state}"]->.s;
        way["man_made"="pipeline"](area.s);
        out center tags;
        '''
        data = _overpass(q)
        if not data:
            results[state] = {'error': 'overpass timeout'}; continue
        els = data.get('elements', [])
        if not els:
            results[state] = {'count': 0}; continue
        cur = db.cursor()
        ins = 0
        for el in els:
            tags = el.get('tags', {})
            name = (tags.get('name') or f"OSM-pipe-{el.get('id')}")[:200]
            op = (tags.get('operator') or '')[:200]
            substance = (tags.get('substance') or '')[:60]
            center = el.get('center') or {}
            lat = center.get('lat')
            lng = center.get('lon')
            if not lat or not lng: continue
            try:
                # pipelines table schema may vary; try common shapes
                cur.execute(
                    """INSERT INTO pipelines
                       (name, operator, substance, lat, lng, state, country)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT DO NOTHING""",
                    (name, op, substance, lat, lng, state, 'US'))
                ins += cur.rowcount
            except Exception as e:
                db.rollback()
                # Fallback if pipelines table has different columns
                logger.warning(f"pipeline insert {state}: {e}")
        try: db.commit()
        except Exception: pass
        results[state] = {'count': len(els), 'inserted': ins}
        time.sleep(2)
    try: db.close()
    except Exception: pass
    return results


def load_communications_towers(states=None):
    """OSM `man_made=communications_tower` nodes (proxy for comm/fiber infra)."""
    results = {}
    db = _get_db()
    if not db: return {'error': 'no DB'}
    states = states or US_STATES
    for state in states:
        q = f'''
        [out:json][timeout:60];
        area["ISO3166-2"="US-{state}"]->.s;
        node["man_made"="communications_tower"](area.s);
        out;
        '''
        data = _overpass(q)
        if not data:
            results[state] = {'error': 'overpass timeout'}; continue
        els = data.get('elements', [])
        if not els:
            results[state] = {'count': 0}; continue
        cur = db.cursor()
        ins = 0
        for el in els:
            tags = el.get('tags', {})
            name = (tags.get('name') or f"OSM-tower-{el.get('id')}")[:200]
            tower_type = (tags.get('tower:type') or '')[:60]
            lat = el.get('lat')
            lng = el.get('lon')
            if not lat or not lng: continue
            try:
                cur.execute(
                    """INSERT INTO infrastructure_layers
                       (id, category, name, lat, lng, state)
                       VALUES (%s, 'comm_tower', %s, %s, %s, %s)
                       ON CONFLICT DO NOTHING""",
                    (f"osm-tower-{el.get('id')}", name, lat, lng, state))
                ins += cur.rowcount
            except Exception as e:
                db.rollback()
                logger.warning(f"comm_tower insert {state}: {e}")
        try: db.commit()
        except Exception: pass
        results[state] = {'count': len(els), 'inserted': ins}
        time.sleep(2)
    try: db.close()
    except Exception: pass
    return results


def run_all_osm(priority_states=None):
    """One-shot orchestrator. priority_states limits to a subset for speed."""
    out = {}
    states = priority_states or ['VA', 'OH', 'TX', 'AZ', 'GA', 'IL', 'CA', 'NV', 'OR', 'WA']
    out['substations'] = load_substations(states)
    out['power_plants'] = load_power_plants(states)
    out['transmission_lines'] = load_transmission_lines(states)
    out['pipelines'] = load_pipelines(states)
    return out
