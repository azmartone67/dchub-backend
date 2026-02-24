"""
Water, Drought & Gas Infrastructure Intelligence Module
========================================================
Endpoints:
  /api/v1/water/streamflow     — USGS real-time streamflow nearby
  /api/v1/water/groundwater    — USGS groundwater levels nearby
  /api/v1/water/drought        — US Drought Monitor severity by county/state
  /api/v1/water/flood-risk     — NOAA streamflow forecasts + flood gauges
  /api/v1/energy/gas-infra     — NASA HIFLD gas compressors, LNG, storage
  /api/v1/water/summary        — Module summary

Data Sources:
  - USGS Water Data API (api.waterdata.usgs.gov) — modernized OGC API
  - USGS WaterServices (waterservices.usgs.gov) — legacy but reliable
  - US Drought Monitor (usdmdataservices.unl.edu) — weekly D0-D4
  - NOAA Water Prediction Service (water.noaa.gov)
  - NASA HIFLD Open Energy FeatureServer (maps.nccs.nasa.gov)
"""

import json
import math
import logging
import os
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
_cache = {}
_CACHE_MAX = 100

def _get_cached(key, ttl_minutes=30):
    if key in _cache:
        entry = _cache[key]
        if (datetime.now(timezone.utc) - entry['ts']).total_seconds() < ttl_minutes * 60:
            return entry['data']
    return None

def _set_cached(key, data):
    if len(_cache) >= _CACHE_MAX:
        oldest = min(_cache, key=lambda k: _cache[k]['ts'])
        del _cache[oldest]
    _cache[key] = {'data': data, 'ts': datetime.now(timezone.utc)}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _safe_float(v):
    try:
        return float(v) if v is not None else None
    except (ValueError, TypeError):
        return None

def _haversine(lat1, lng1, lat2, lng2):
    R = 3959  # miles
    dlat = math.radians((lat2 or 0) - (lat1 or 0))
    dlng = math.radians((lng2 or 0) - (lng1 or 0))
    a = (math.sin(dlat/2)**2 +
         math.cos(math.radians(lat1 or 0)) * math.cos(math.radians(lat2 or 0)) *
         math.sin(dlng/2)**2)
    return R * 2 * math.asin(math.sqrt(min(1, a)))

def _fetch_json(url, timeout=30, headers=None):
    """Generic JSON fetch."""
    hdrs = {'User-Agent': 'DCHub/1.0', 'Accept': 'application/json'}
    if headers:
        hdrs.update(headers)
    try:
        req = urllib.request.Request(url, headers=hdrs)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        logger.warning(f"Fetch error ({url[:120]}): {e}")
        return {'error': str(e)}

def _bbox_degrees(lat, lng, radius_miles):
    """Return min_lat, max_lat, min_lng, max_lng."""
    lat_per_mile = 1.0 / 69.0
    lng_per_mile = 1.0 / (69.0 * math.cos(math.radians(lat)))
    return (
        lat - radius_miles * lat_per_mile,
        lat + radius_miles * lat_per_mile,
        lng - radius_miles * lng_per_mile,
        lng + radius_miles * lng_per_mile,
    )

# State lookup from coordinates (for drought monitor county FIPS)
_STATE_FIPS = {
    'AL':'01','AK':'02','AZ':'04','AR':'05','CA':'06','CO':'08','CT':'09',
    'DE':'10','FL':'12','GA':'13','HI':'15','ID':'16','IL':'17','IN':'18',
    'IA':'19','KS':'20','KY':'21','LA':'22','ME':'23','MD':'24','MA':'25',
    'MI':'26','MN':'27','MS':'28','MO':'29','MT':'30','NE':'31','NV':'32',
    'NH':'33','NJ':'34','NM':'35','NY':'36','NC':'37','ND':'38','OH':'39',
    'OK':'40','OR':'41','PA':'42','RI':'44','SC':'45','SD':'46','TN':'47',
    'TX':'48','UT':'49','VT':'50','VA':'51','WA':'53','WV':'54','WI':'55',
    'WY':'56','DC':'11',
}

_STATE_CENTERS = {
    'AZ': (34.05, -111.09), 'CA': (36.78, -119.42), 'TX': (31.97, -99.90),
    'NV': (38.80, -116.42), 'NM': (34.52, -105.87), 'CO': (39.55, -105.78),
    'UT': (39.32, -111.09), 'FL': (27.66, -81.52), 'GA': (32.16, -82.90),
    'VA': (37.43, -78.66), 'NC': (35.76, -79.02), 'OH': (40.42, -82.91),
    'IL': (40.63, -89.40), 'PA': (41.20, -77.19), 'NY': (43.30, -74.22),
    'NJ': (40.06, -74.41), 'WA': (47.75, -120.74), 'OR': (43.80, -120.55),
    'ID': (44.07, -114.74), 'MT': (46.80, -110.36), 'WY': (43.08, -107.29),
    'ND': (47.55, -101.00), 'SD': (43.97, -99.90), 'NE': (41.49, -99.90),
    'KS': (38.50, -98.00), 'OK': (35.47, -97.52), 'MN': (46.73, -94.69),
    'IA': (41.88, -93.10), 'MO': (38.57, -92.60), 'AR': (34.97, -92.37),
    'LA': (31.17, -91.87), 'MS': (32.35, -89.40), 'AL': (32.32, -86.90),
    'SC': (33.84, -81.16), 'TN': (35.52, -86.58), 'KY': (37.67, -84.67),
    'WV': (38.60, -80.95), 'IN': (40.27, -86.13), 'MI': (44.35, -85.41),
    'WI': (43.78, -88.79), 'MA': (42.41, -71.38), 'CT': (41.60, -72.76),
    'MD': (39.05, -76.64), 'ME': (45.25, -69.45),
}

def _estimate_state(lat, lng):
    best, best_dist = None, 9999
    for st, (slat, slng) in _STATE_CENTERS.items():
        d = _haversine(lat, lng, slat, slng)
        if d < best_dist:
            best_dist, best = d, st
    return best


# =============================================================================
# Registration function (called by main.py)
# =============================================================================
def register_water_routes(app):
    from flask import request, jsonify

    # -----------------------------------------------------------------
    # 1. USGS Real-Time Streamflow Nearby
    # -----------------------------------------------------------------
    @app.route('/api/v1/water/streamflow')
    def usgs_streamflow_nearby():
        """Real-time streamflow (discharge) from USGS monitoring sites nearby.

        Query params:
            lat (float): Latitude (required)
            lng (float): Longitude (required)
            radius (float): Search radius in miles (default: 30)
        """
        lat = request.args.get('lat', type=float)
        lng = request.args.get('lng', type=float)
        if lat is None or lng is None:
            return jsonify({'error': 'lat and lng parameters required'}), 400

        radius = request.args.get('radius', 30, type=float)
        cache_key = f"usgs_flow_{lat:.1f}_{lng:.1f}_{radius}"
        cached = _get_cached(cache_key, ttl_minutes=15)
        if cached:
            return jsonify(cached)

        # USGS WaterServices — instantaneous values for discharge (param 00060)
        min_lat, max_lat, min_lng, max_lng = _bbox_degrees(lat, lng, radius)
        params = {
            'format': 'json',
            'bBox': f'{min_lng:.4f},{min_lat:.4f},{max_lng:.4f},{max_lat:.4f}',
            'parameterCd': '00060',  # Discharge (cfs)
            'siteStatus': 'active',
            'siteType': 'ST',  # Streams
        }
        url = f"https://waterservices.usgs.gov/nwis/iv/?{urllib.parse.urlencode(params)}"
        data = _fetch_json(url, timeout=20)

        if 'error' in data:
            return jsonify({
                'success': False, 'error': data['error'],
                'note': 'USGS WaterServices may be temporarily unavailable'
            }), 502

        # Parse USGS response
        ts_list = data.get('value', {}).get('timeSeries', [])
        sites = []
        for ts in ts_list:
            site_info = ts.get('sourceInfo', {})
            geo = site_info.get('geoLocation', {}).get('geogLocation', {})
            site_lat = _safe_float(geo.get('latitude'))
            site_lng = _safe_float(geo.get('longitude'))

            if site_lat and site_lng:
                dist = _haversine(lat, lng, site_lat, site_lng)
                if dist > radius:
                    continue
            else:
                continue

            values = ts.get('values', [{}])[0].get('value', [])
            latest = values[-1] if values else {}

            sites.append({
                'site_code': site_info.get('siteCode', [{}])[0].get('value', ''),
                'site_name': site_info.get('siteName', ''),
                'latitude': site_lat,
                'longitude': site_lng,
                'distance_miles': round(dist, 1),
                'discharge_cfs': _safe_float(latest.get('value')),
                'datetime': latest.get('dateTime', ''),
                'qualifier': latest.get('qualifiers', [''])[0] if latest.get('qualifiers') else '',
            })

        sites.sort(key=lambda x: x['distance_miles'])

        result = {
            'success': True,
            'source': 'USGS WaterServices (Instantaneous Values)',
            'parameter': 'Discharge (cfs) — 00060',
            'query': {'lat': lat, 'lng': lng, 'radius_miles': radius},
            'total_sites': len(sites),
            'sites': sites[:30],
            'data_center_note': 'Streamflow indicates water availability for cooling systems. Low discharge = potential water stress.',
            'queried_at': datetime.now(timezone.utc).isoformat(),
        }
        _set_cached(cache_key, result)
        return jsonify(result)

    # -----------------------------------------------------------------
    # 2. USGS Groundwater Levels Nearby
    # -----------------------------------------------------------------
    @app.route('/api/v1/water/groundwater')
    def usgs_groundwater_nearby():
        """Groundwater levels from USGS monitoring wells nearby.

        Query params:
            lat (float): Latitude (required)
            lng (float): Longitude (required)
            radius (float): Search radius in miles (default: 30)
        """
        lat = request.args.get('lat', type=float)
        lng = request.args.get('lng', type=float)
        if lat is None or lng is None:
            return jsonify({'error': 'lat and lng parameters required'}), 400

        radius = request.args.get('radius', 30, type=float)
        cache_key = f"usgs_gw_{lat:.1f}_{lng:.1f}_{radius}"
        cached = _get_cached(cache_key, ttl_minutes=30)
        if cached:
            return jsonify(cached)

        min_lat, max_lat, min_lng, max_lng = _bbox_degrees(lat, lng, radius)
        params = {
            'format': 'json',
            'bBox': f'{min_lng:.4f},{min_lat:.4f},{max_lng:.4f},{max_lat:.4f}',
            'parameterCd': '72019',  # Depth to water level (ft below surface)
            'siteStatus': 'active',
            'siteType': 'GW',  # Groundwater wells
        }
        url = f"https://waterservices.usgs.gov/nwis/iv/?{urllib.parse.urlencode(params)}"
        data = _fetch_json(url, timeout=20)

        if 'error' in data:
            # Fallback: try groundwater levels service
            params['parameterCd'] = '62610'  # Alternate GW depth code
            url2 = f"https://waterservices.usgs.gov/nwis/iv/?{urllib.parse.urlencode(params)}"
            data = _fetch_json(url2, timeout=20)
            if 'error' in data:
                return jsonify({
                    'success': False, 'error': data['error'],
                    'note': 'USGS groundwater service may be unavailable'
                }), 502

        ts_list = data.get('value', {}).get('timeSeries', [])
        wells = []
        for ts in ts_list:
            site_info = ts.get('sourceInfo', {})
            geo = site_info.get('geoLocation', {}).get('geogLocation', {})
            site_lat = _safe_float(geo.get('latitude'))
            site_lng = _safe_float(geo.get('longitude'))

            if site_lat and site_lng:
                dist = _haversine(lat, lng, site_lat, site_lng)
                if dist > radius:
                    continue
            else:
                continue

            values = ts.get('values', [{}])[0].get('value', [])
            latest = values[-1] if values else {}
            var_info = ts.get('variable', {})

            wells.append({
                'site_code': site_info.get('siteCode', [{}])[0].get('value', ''),
                'site_name': site_info.get('siteName', ''),
                'latitude': site_lat,
                'longitude': site_lng,
                'distance_miles': round(dist, 1),
                'depth_to_water_ft': _safe_float(latest.get('value')),
                'unit': var_info.get('unit', {}).get('unitCode', 'ft'),
                'parameter': var_info.get('variableName', ''),
                'datetime': latest.get('dateTime', ''),
            })

        wells.sort(key=lambda x: x['distance_miles'])

        result = {
            'success': True,
            'source': 'USGS WaterServices (Groundwater Levels)',
            'query': {'lat': lat, 'lng': lng, 'radius_miles': radius},
            'total_wells': len(wells),
            'wells': wells[:30],
            'data_center_note': 'Groundwater depth indicates aquifer availability for cooling. Deeper water = higher extraction costs.',
            'queried_at': datetime.now(timezone.utc).isoformat(),
        }
        _set_cached(cache_key, result)
        return jsonify(result)

    # -----------------------------------------------------------------
    # 3. US Drought Monitor — County/State Severity
    # -----------------------------------------------------------------
    @app.route('/api/v1/water/drought')
    def drought_monitor():
        """Current US Drought Monitor severity for a state or county.

        Query params:
            state (str): Two-letter state code (required, or use lat/lng)
            lat (float): Latitude (auto-detects state)
            lng (float): Longitude (auto-detects state)
            weeks (int): Number of weeks of history (default: 4)
        """
        state = request.args.get('state', '').upper()
        lat = request.args.get('lat', type=float)
        lng = request.args.get('lng', type=float)
        weeks = min(request.args.get('weeks', 4, type=int), 52)

        if not state and lat and lng:
            state = _estimate_state(lat, lng)

        if not state:
            return jsonify({'error': 'state parameter or lat/lng required'}), 400

        cache_key = f"drought_{state}_{weeks}"
        cached = _get_cached(cache_key, ttl_minutes=60)
        if cached:
            return jsonify(cached)

        # Convert state abbreviation to FIPS code — USDM API accepts both
        # but FIPS is more reliable
        state_fips = _STATE_FIPS.get(state, state)

        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(weeks=weeks)

        # US Drought Monitor REST API — state-level comprehensive stats
        sd = f"{start_date.month}/{start_date.day}/{start_date.year}"
        ed = f"{end_date.month}/{end_date.day}/{end_date.year}"
        
        # Try both FIPS and abbreviation
        urls_to_try = [
            (
                f"https://usdmdataservices.unl.edu/api/StateStatistics/"
                f"GetDroughtSeverityStatisticsByAreaPercent"
                f"?aoi={state_fips}&startdate={sd}&enddate={ed}&statisticsType=1"
            ),
            (
                f"https://usdmdataservices.unl.edu/api/StateStatistics/"
                f"GetDroughtSeverityStatisticsByAreaPercent"
                f"?aoi={state}&startdate={sd}&enddate={ed}&statisticsType=1"
            ),
        ]

        dm_data = None
        last_error = None
        for dm_url in urls_to_try:
            try:
                logger.info(f"Drought Monitor trying: {dm_url}")
                req = urllib.request.Request(dm_url, headers={
                    'User-Agent': 'DCHub/1.0',
                    'Accept': 'application/json',
                    'Content-Type': 'application/json',
                })
                with urllib.request.urlopen(req, timeout=15) as resp:
                    content_type = resp.headers.get('Content-Type', '')
                    raw = resp.read().decode('utf-8')
                    logger.info(f"USDM response: {len(raw)} bytes, type={content_type}, preview={raw[:200]}")
                    
                    if raw.strip().startswith('<'):
                        logger.warning("USDM returned XML/HTML, skipping")
                        last_error = 'API returned XML instead of JSON'
                        continue
                    if raw.strip().startswith('[') or raw.strip().startswith('{'):
                        dm_data = json.loads(raw)
                        if dm_data:
                            break
            except Exception as e:
                last_error = str(e)
                logger.warning(f"USDM error: {e}")

        if not dm_data:
            return jsonify({
                'success': False,
                'error': last_error or 'US Drought Monitor API unavailable',
                'debug_urls_tried': urls_to_try,
                'source_url': 'https://droughtmonitor.unl.edu/',
            }), 502

        # Parse weekly data — most recent first
        weekly = []
        for entry in dm_data:
            map_date = entry.get('MapDate', entry.get('mapDate', ''))
            weekly.append({
                'date': map_date,
                'none_pct': _safe_float(entry.get('None', entry.get('none', 0))),
                'd0_pct': _safe_float(entry.get('D0', entry.get('d0', 0))),
                'd1_pct': _safe_float(entry.get('D1', entry.get('d1', 0))),
                'd2_pct': _safe_float(entry.get('D2', entry.get('d2', 0))),
                'd3_pct': _safe_float(entry.get('D3', entry.get('d3', 0))),
                'd4_pct': _safe_float(entry.get('D4', entry.get('d4', 0))),
            })

        # Most recent week summary
        latest = weekly[0] if weekly else {}
        drought_pct = round(100 - (latest.get('none_pct') or 0), 1)

        severity = 'None'
        if (latest.get('d4_pct') or 0) > 5:
            severity = 'Exceptional (D4)'
        elif (latest.get('d3_pct') or 0) > 10:
            severity = 'Extreme (D3)'
        elif (latest.get('d2_pct') or 0) > 15:
            severity = 'Severe (D2)'
        elif (latest.get('d1_pct') or 0) > 20:
            severity = 'Moderate (D1)'
        elif (latest.get('d0_pct') or 0) > 20:
            severity = 'Abnormally Dry (D0)'

        result = {
            'success': True,
            'source': 'US Drought Monitor (NDMC/USDA/NOAA/NASA)',
            'source_url': 'https://droughtmonitor.unl.edu/',
            'state': state,
            'state_fips': _STATE_FIPS.get(state, ''),
            'current_drought_pct': drought_pct,
            'dominant_severity': severity,
            'latest_week': latest,
            'weekly_history': weekly[:weeks],
            'drought_categories': {
                'D0': 'Abnormally Dry',
                'D1': 'Moderate Drought',
                'D2': 'Severe Drought',
                'D3': 'Extreme Drought',
                'D4': 'Exceptional Drought',
            },
            'data_center_note': (
                'Drought severity directly impacts cooling water availability. '
                'D2+ regions face potential water use restrictions affecting DC operations.'
            ),
            'queried_at': datetime.now(timezone.utc).isoformat(),
        }
        _set_cached(cache_key, result)
        return jsonify(result)

    # -----------------------------------------------------------------
    # 4. NOAA Flood Risk — Stream Gauges & Forecasts
    # -----------------------------------------------------------------
    @app.route('/api/v1/water/flood-risk')
    def noaa_flood_risk():
        """NOAA streamflow gauges and flood status near coordinates.

        Query params:
            lat (float): Latitude (required)
            lng (float): Longitude (required)
            radius (float): Search radius in miles (default: 30)
        """
        lat = request.args.get('lat', type=float)
        lng = request.args.get('lng', type=float)
        if lat is None or lng is None:
            return jsonify({'error': 'lat and lng parameters required'}), 400

        radius = request.args.get('radius', 30, type=float)
        cache_key = f"noaa_flood_{lat:.1f}_{lng:.1f}_{radius}"
        cached = _get_cached(cache_key, ttl_minutes=15)
        if cached:
            return jsonify(cached)

        # NOAA NWPS API — nearby gauges
        # Get USGS sites with gage height (param 00065) in bbox
        min_lat, max_lat, min_lng, max_lng = _bbox_degrees(lat, lng, radius)
        params = {
            'format': 'json',
            'bBox': f'{min_lng:.4f},{min_lat:.4f},{max_lng:.4f},{max_lat:.4f}',
            'parameterCd': '00065',  # Gage height (ft)
            'siteStatus': 'active',
            'siteType': 'ST',
        }
        url = f"https://waterservices.usgs.gov/nwis/iv/?{urllib.parse.urlencode(params)}"
        data = _fetch_json(url, timeout=20)

        if 'error' in data:
            return jsonify({
                'success': False, 'error': data['error'],
                'note': 'USGS gage height service may be unavailable'
            }), 502

        ts_list = data.get('value', {}).get('timeSeries', [])
        gauges = []
        for ts in ts_list:
            site_info = ts.get('sourceInfo', {})
            geo = site_info.get('geoLocation', {}).get('geogLocation', {})
            site_lat = _safe_float(geo.get('latitude'))
            site_lng = _safe_float(geo.get('longitude'))

            if site_lat and site_lng:
                dist = _haversine(lat, lng, site_lat, site_lng)
                if dist > radius:
                    continue
            else:
                continue

            values = ts.get('values', [{}])[0].get('value', [])
            latest = values[-1] if values else {}

            site_code = site_info.get('siteCode', [{}])[0].get('value', '')

            gauges.append({
                'site_code': site_code,
                'site_name': site_info.get('siteName', ''),
                'latitude': site_lat,
                'longitude': site_lng,
                'distance_miles': round(dist, 1),
                'gage_height_ft': _safe_float(latest.get('value')),
                'datetime': latest.get('dateTime', ''),
                'nwps_url': f'https://water.noaa.gov/gauges/{site_code}',
            })

        gauges.sort(key=lambda x: x['distance_miles'])

        # Classify flood risk based on gage heights
        high_gauges = [g for g in gauges if (g.get('gage_height_ft') or 0) > 20]
        risk_level = 'Low'
        if len(high_gauges) > 2:
            risk_level = 'High'
        elif len(high_gauges) > 0:
            risk_level = 'Moderate'

        result = {
            'success': True,
            'source': 'USGS WaterServices (Gage Height) + NOAA NWPS',
            'query': {'lat': lat, 'lng': lng, 'radius_miles': radius},
            'flood_risk_level': risk_level,
            'total_gauges': len(gauges),
            'elevated_gauges': len(high_gauges),
            'gauges': gauges[:20],
            'data_center_note': (
                'Gage heights indicate current flood potential. '
                'Sites near gauges with elevated readings face higher flood risk.'
            ),
            'queried_at': datetime.now(timezone.utc).isoformat(),
        }
        _set_cached(cache_key, result)
        return jsonify(result)

    # -----------------------------------------------------------------
    # 5. NASA HIFLD Gas Infrastructure (Compressors, LNG, Storage)
    # -----------------------------------------------------------------
    @app.route('/api/v1/energy/gas-infra')
    def hifld_gas_infrastructure():
        """Gas infrastructure from NASA HIFLD Open Energy FeatureServer.
        Includes compressor stations, LNG terminals, gas storage, gas processing plants.

        Query params:
            lat (float): Latitude (required)
            lng (float): Longitude (required)
            radius (float): Search radius in miles (default: 50)
            type (str): Filter: compressor, lng, storage, processing (default: all)
        """
        lat = request.args.get('lat', type=float)
        lng = request.args.get('lng', type=float)
        if lat is None or lng is None:
            return jsonify({'error': 'lat and lng parameters required'}), 400

        radius = request.args.get('radius', 50, type=float)
        infra_type = request.args.get('type', '').lower()

        cache_key = f"hifld_gas_{lat:.1f}_{lng:.1f}_{radius}_{infra_type}"
        cached = _get_cached(cache_key, ttl_minutes=60)
        if cached:
            return jsonify(cached)

        base_url = 'https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/energy/FeatureServer'
        # Fallback: HIFLD data also hosted on ArcGIS Online
        fallback_url = 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services'
        
        # Layer config: NASA IDs + ArcGIS Online service names
        layers = {
            'compressor': {
                'id': 6,
                'label': 'Natural Gas Compressor Stations',
                'arcgis_service': 'Natural_Gas_Compressor_Stations',
            },
            'storage': {
                'id': 19,
                'label': 'Natural Gas Storage Facilities',
                'arcgis_service': 'Natural_Gas_Storage',
            },
        }

        if infra_type and infra_type in layers:
            query_layers = {infra_type: layers[infra_type]}
        else:
            query_layers = layers

        min_lat, max_lat, min_lng, max_lng = _bbox_degrees(lat, lng, radius)
        bbox = f'{min_lng},{min_lat},{max_lng},{max_lat}'

        all_facilities = []
        layer_counts = {}
        errors = []

        params = {
            'geometry': bbox,
            'geometryType': 'esriGeometryEnvelope',
            'inSR': '4326',
            'spatialRel': 'esriSpatialRelIntersects',
            'outFields': '*',
            'returnGeometry': 'true',
            'outSR': '4326',
            'f': 'json',
            'resultRecordCount': '100',
        }

        for ltype, linfo in query_layers.items():
            # Try NASA first, then ArcGIS Online fallback
            urls_to_try = [
                f"{base_url}/{linfo['id']}/query",
            ]
            if 'arcgis_service' in linfo:
                urls_to_try.append(
                    f"{fallback_url}/{linfo['arcgis_service']}/FeatureServer/0/query"
                )
            
            resp = None
            for try_url in urls_to_try:
                q_url = f"{try_url}?{urllib.parse.urlencode(params)}"
                logger.info(f"HIFLD gas query: {ltype} → {try_url[:80]}")
                resp = _fetch_json(q_url, timeout=15)
                if 'features' in resp:
                    break
                logger.warning(f"HIFLD {ltype} failed: {resp.get('error', 'no features')}")
                resp = None
            
            if resp is None:
                errors.append(f"{ltype}: all sources failed")
                layer_counts[ltype] = 0
                continue

            features = resp.get('features', [])
            count = 0
            for feat in features:
                attrs = feat.get('attributes', {})
                geom = feat.get('geometry', {})
                f_lat = _safe_float(geom.get('y'))
                f_lng = _safe_float(geom.get('x'))

                if f_lat and f_lng:
                    dist = _haversine(lat, lng, f_lat, f_lng)
                    if dist > radius:
                        continue
                else:
                    continue

                name = (attrs.get('NAME') or attrs.get('name') or
                        attrs.get('OPERATOR') or attrs.get('operator') or 'Unknown')

                facility = {
                    'type': ltype,
                    'type_label': linfo['label'],
                    'name': name,
                    'operator': attrs.get('OPERATOR') or attrs.get('operator', ''),
                    'state': attrs.get('STATE') or attrs.get('state', ''),
                    'county': attrs.get('COUNTY') or attrs.get('county', ''),
                    'latitude': f_lat,
                    'longitude': f_lng,
                    'distance_miles': round(dist, 1),
                    'status': attrs.get('STATUS') or attrs.get('status', ''),
                }

                # Add type-specific fields
                if ltype == 'compressor':
                    facility['capacity_hp'] = attrs.get('CAPACITY_H') or attrs.get('capacity_h')
                elif ltype == 'storage':
                    facility['capacity_bcf'] = attrs.get('CAPACITY_B') or attrs.get('capacity_b')
                    facility['storage_type'] = attrs.get('TYPE_FACIL') or attrs.get('type_facil', '')
                elif ltype == 'lng':
                    facility['terminal_type'] = attrs.get('TYPE') or attrs.get('type', '')

                all_facilities.append(facility)
                count += 1

            layer_counts[ltype] = count

        all_facilities.sort(key=lambda x: x['distance_miles'])

        result = {
            'success': True,
            'source': 'NASA HIFLD Open Energy FeatureServer',
            'source_url': base_url,
            'query': {'lat': lat, 'lng': lng, 'radius_miles': radius, 'type_filter': infra_type or 'all'},
            'total_facilities': len(all_facilities),
            'by_type': layer_counts,
            'facilities': all_facilities[:50],
            'errors': errors if errors else None,
            'data_center_note': (
                'Gas infrastructure proximity supports on-site power generation. '
                'Compressor stations indicate pipeline pressure points; '
                'LNG terminals offer alternative fuel supply; '
                'Storage facilities provide supply buffer during demand spikes.'
            ),
            'queried_at': datetime.now(timezone.utc).isoformat(),
        }
        _set_cached(cache_key, result)
        return jsonify(result)

    # -----------------------------------------------------------------
    # 6. Module Summary
    # -----------------------------------------------------------------
    @app.route('/api/v1/water/summary')
    def water_drought_summary():
        """Water, Drought & Gas Infrastructure module summary."""
        return jsonify({
            'module': 'Water, Drought & Gas Infrastructure Intelligence',
            'version': '1.0',
            'endpoints': {
                '/api/v1/water/streamflow': {
                    'description': 'USGS real-time streamflow (discharge) nearby',
                    'source': 'USGS WaterServices',
                    'params': 'lat, lng, radius',
                    'dc_relevance': 'Cooling water availability indicator',
                },
                '/api/v1/water/groundwater': {
                    'description': 'USGS groundwater levels from monitoring wells',
                    'source': 'USGS WaterServices',
                    'params': 'lat, lng, radius',
                    'dc_relevance': 'Aquifer depth for well-water cooling systems',
                },
                '/api/v1/water/drought': {
                    'description': 'US Drought Monitor severity (D0-D4) by state',
                    'source': 'USDM (NDMC/USDA/NOAA/NASA)',
                    'params': 'state OR lat/lng, weeks',
                    'dc_relevance': 'Water restriction risk — D2+ may limit DC cooling',
                },
                '/api/v1/water/flood-risk': {
                    'description': 'Stream gage heights and flood risk assessment',
                    'source': 'USGS WaterServices + NOAA NWPS',
                    'params': 'lat, lng, radius',
                    'dc_relevance': 'Flood exposure for low-lying DC sites',
                },
                '/api/v1/energy/gas-infra': {
                    'description': 'Gas compressors, LNG terminals, storage, processing plants',
                    'source': 'NASA HIFLD Open Energy FeatureServer',
                    'params': 'lat, lng, radius, type',
                    'dc_relevance': 'On-site generation fuel availability',
                },
            },
            'data_sources': [
                {'name': 'USGS WaterServices', 'url': 'https://waterservices.usgs.gov/', 'update': 'Real-time (15-min)'},
                {'name': 'US Drought Monitor', 'url': 'https://droughtmonitor.unl.edu/', 'update': 'Weekly (Thursday)'},
                {'name': 'NOAA Water Prediction', 'url': 'https://water.noaa.gov/', 'update': 'Real-time'},
                {'name': 'NASA HIFLD Energy', 'url': 'https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/energy/FeatureServer', 'update': 'Periodic'},
            ],
        })

    # Log registration
    logger.info("💧 Water & Drought Intelligence: ✅ Registered")
    logger.info("   📍 /api/v1/water/streamflow — USGS real-time discharge")
    logger.info("   📍 /api/v1/water/groundwater — USGS well water levels")
    logger.info("   📍 /api/v1/water/drought — US Drought Monitor (D0-D4)")
    logger.info("   📍 /api/v1/water/flood-risk — Stream gauges + flood risk")
    logger.info("   📍 /api/v1/energy/gas-infra — NASA HIFLD gas infrastructure")
    logger.info("   📊 Sources: USGS, USDM, NOAA NWPS, NASA HIFLD")
