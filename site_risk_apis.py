import requests
import math
import time
import json
import os
import logging
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)

site_risk_bp = Blueprint('site_risk', __name__)

CACHE = {}
CACHE_DURATION = 1800
COMPOSITE_CACHE_DURATION = 86400

def get_cached(key, ttl=None):
    if key in CACHE:
        data, ts = CACHE[key]
        duration = ttl if ttl is not None else CACHE_DURATION
        if time.time() - ts < duration:
            return data
    return None

def set_cache(key, data, ttl=None):
    CACHE[key] = (data, time.time())
    return data

def haversine(lat1, lng1, lat2, lng2):
    R = 3959
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng/2)**2
    return R * 2 * math.asin(math.sqrt(a))


# ─── USGS WATER DATA API ─────────────────────────────────────────────────────

class USGSWaterAPI:
    BASE_URL = "https://waterservices.usgs.gov/nwis"

    @classmethod
    def get_groundwater_sites(cls, lat, lng, radius_miles=50):
        cache_key = f"usgs_gw_{lat:.2f}_{lng:.2f}_{radius_miles}"
        cached = get_cached(cache_key)
        if cached:
            return cached

        try:
            params = {
                'format': 'json',
                'bBox': cls._bbox(lat, lng, radius_miles),
                'siteType': 'GW',
                'siteStatus': 'active',
                'hasDataTypeCd': 'gw',
            }
            resp = requests.get(f"{cls.BASE_URL}/site/", params=params, timeout=15)
            if resp.status_code != 200:
                return cls._fallback_groundwater(lat, lng)

            data = resp.json()
            sites = []
            for site in data.get('value', {}).get('timeSeries', []):
                site_info = site.get('sourceInfo', {})
                geo = site_info.get('geoLocation', {}).get('geogLocation', {})
                sites.append({
                    'site_id': site_info.get('siteCode', [{}])[0].get('value', ''),
                    'name': site_info.get('siteName', ''),
                    'lat': geo.get('latitude', 0),
                    'lng': geo.get('longitude', 0),
                    'type': 'groundwater',
                    'county': site_info.get('siteProperty', [{}])[0].get('value', ''),
                })
            return set_cache(cache_key, sites)
        except Exception as e:
            logger.warning(f"USGS groundwater API error: {e}")
            return cls._fallback_groundwater(lat, lng)

    @classmethod
    def get_water_levels(cls, lat, lng, radius_miles=30):
        cache_key = f"usgs_wl_{lat:.2f}_{lng:.2f}_{radius_miles}"
        cached = get_cached(cache_key)
        if cached:
            return cached

        try:
            bbox = cls._bbox(lat, lng, radius_miles)
            params = {
                'format': 'json',
                'bBox': bbox,
                'siteType': 'GW',
                'siteStatus': 'active',
                'parameterCd': '72019',
                'period': 'P30D',
            }
            resp = requests.get(f"{cls.BASE_URL}/iv/", params=params, timeout=15)
            if resp.status_code != 200:
                return cls._fallback_water_levels(lat, lng)

            data = resp.json()
            results = []
            for ts in data.get('value', {}).get('timeSeries', []):
                source = ts.get('sourceInfo', {})
                geo = source.get('geoLocation', {}).get('geogLocation', {})
                values = ts.get('values', [{}])[0].get('value', [])
                latest = values[-1] if values else {}

                results.append({
                    'site_id': source.get('siteCode', [{}])[0].get('value', ''),
                    'name': source.get('siteName', ''),
                    'lat': geo.get('latitude', 0),
                    'lng': geo.get('longitude', 0),
                    'depth_ft': float(latest.get('value', 0)) if latest.get('value') else None,
                    'measured_at': latest.get('dateTime', ''),
                    'distance_miles': haversine(lat, lng, geo.get('latitude', 0), geo.get('longitude', 0)),
                })

            results.sort(key=lambda x: x['distance_miles'])
            return set_cache(cache_key, results[:25])
        except Exception as e:
            logger.warning(f"USGS water levels API error: {e}")
            return cls._fallback_water_levels(lat, lng)

    @classmethod
    def get_streamflow(cls, lat, lng, radius_miles=30):
        cache_key = f"usgs_sf_{lat:.2f}_{lng:.2f}_{radius_miles}"
        cached = get_cached(cache_key)
        if cached:
            return cached

        try:
            bbox = cls._bbox(lat, lng, radius_miles)
            params = {
                'format': 'json',
                'bBox': bbox,
                'siteType': 'ST',
                'siteStatus': 'active',
                'parameterCd': '00060',
                'period': 'P7D',
            }
            resp = requests.get(f"{cls.BASE_URL}/iv/", params=params, timeout=15)
            if resp.status_code != 200:
                return cls._fallback_streamflow(lat, lng)

            data = resp.json()
            results = []
            for ts in data.get('value', {}).get('timeSeries', []):
                source = ts.get('sourceInfo', {})
                geo = source.get('geoLocation', {}).get('geogLocation', {})
                values = ts.get('values', [{}])[0].get('value', [])
                latest = values[-1] if values else {}
                flow_values = [float(v.get('value', 0)) for v in values if v.get('value')]

                results.append({
                    'site_id': source.get('siteCode', [{}])[0].get('value', ''),
                    'name': source.get('siteName', ''),
                    'lat': geo.get('latitude', 0),
                    'lng': geo.get('longitude', 0),
                    'current_flow_cfs': float(latest.get('value', 0)) if latest.get('value') else None,
                    'avg_flow_7d_cfs': round(sum(flow_values) / len(flow_values), 1) if flow_values else None,
                    'max_flow_7d_cfs': max(flow_values) if flow_values else None,
                    'min_flow_7d_cfs': min(flow_values) if flow_values else None,
                    'measured_at': latest.get('dateTime', ''),
                    'distance_miles': haversine(lat, lng, geo.get('latitude', 0), geo.get('longitude', 0)),
                })

            results.sort(key=lambda x: x['distance_miles'])
            return set_cache(cache_key, results[:20])
        except Exception as e:
            logger.warning(f"USGS streamflow API error: {e}")
            return cls._fallback_streamflow(lat, lng)

    @classmethod
    def calculate_water_stress(cls, lat, lng):
        cache_key = f"water_stress_{lat:.2f}_{lng:.2f}"
        cached = get_cached(cache_key)
        if cached:
            return cached

        gw_sites = cls.get_groundwater_sites(lat, lng, 50)
        water_levels = cls.get_water_levels(lat, lng, 30)
        streamflow = cls.get_streamflow(lat, lng, 30)

        gw_count = len(gw_sites) if isinstance(gw_sites, list) else 0
        wl_count = len(water_levels) if isinstance(water_levels, list) else 0
        sf_count = len(streamflow) if isinstance(streamflow, list) else 0

        score = 50

        if gw_count >= 10:
            score += 15
        elif gw_count >= 5:
            score += 10
        elif gw_count >= 1:
            score += 5
        else:
            score -= 15

        if wl_count > 0 and isinstance(water_levels, list):
            avg_depth = sum(w.get('depth_ft', 0) or 0 for w in water_levels) / max(wl_count, 1)
            if avg_depth < 50:
                score += 15
            elif avg_depth < 100:
                score += 10
            elif avg_depth < 200:
                score += 5
            else:
                score -= 10
        else:
            score -= 5

        if sf_count > 0 and isinstance(streamflow, list):
            avg_flow = sum(s.get('current_flow_cfs', 0) or 0 for s in streamflow) / max(sf_count, 1)
            if avg_flow > 1000:
                score += 15
            elif avg_flow > 100:
                score += 10
            elif avg_flow > 10:
                score += 5
            else:
                score -= 5
        else:
            score -= 5

        score = max(0, min(100, score))

        if score >= 80:
            risk = 'low'
        elif score >= 60:
            risk = 'moderate'
        elif score >= 40:
            risk = 'elevated'
        else:
            risk = 'high'

        result = {
            'water_availability_score': score,
            'water_stress_risk': risk,
            'groundwater_sites_nearby': gw_count,
            'active_monitoring_wells': wl_count,
            'stream_gauges_nearby': sf_count,
            'assessment': cls._water_assessment(score, gw_count, wl_count, sf_count),
            'data_sources': ['USGS National Water Information System', 'USGS Instantaneous Values Service'],
        }
        return set_cache(cache_key, result)

    @classmethod
    def _water_assessment(cls, score, gw, wl, sf):
        if score >= 80:
            return f"Excellent water availability. {gw} groundwater sources, {sf} streams within range. Low risk for cooling water supply."
        elif score >= 60:
            return f"Adequate water availability. {gw} groundwater sources, {sf} streams nearby. Consider backup water plans."
        elif score >= 40:
            return f"Limited water availability. Only {gw} groundwater sources and {sf} streams detected. Water recycling systems recommended."
        else:
            return f"Water-stressed area. {gw} groundwater sources, {sf} streams. Air-cooled or closed-loop systems strongly recommended."

    @classmethod
    def _bbox(cls, lat, lng, radius_miles):
        deg = radius_miles / 69.0
        return f"{lng - deg:.4f},{lat - deg:.4f},{lng + deg:.4f},{lat + deg:.4f}"

    @classmethod
    def _fallback_groundwater(cls, lat, lng):
        return []

    @classmethod
    def _fallback_water_levels(cls, lat, lng):
        return []

    @classmethod
    def _fallback_streamflow(cls, lat, lng):
        return []


# ─── USGS EARTHQUAKE / SEISMIC API ───────────────────────────────────────────

class USGSEarthquakeAPI:
    BASE_URL = "https://earthquake.usgs.gov/fdsnws/event/1"

    @classmethod
    def get_seismic_history(cls, lat, lng, radius_km=200, years=20):
        cache_key = f"seismic_{lat:.2f}_{lng:.2f}_{radius_km}_{years}"
        cached = get_cached(cache_key)
        if cached:
            return cached

        try:
            end = datetime.utcnow()
            start = end - timedelta(days=365 * years)

            params = {
                'format': 'geojson',
                'latitude': lat,
                'longitude': lng,
                'maxradiuskm': radius_km,
                'starttime': start.strftime('%Y-%m-%d'),
                'endtime': end.strftime('%Y-%m-%d'),
                'minmagnitude': 2.0,
                'orderby': 'magnitude',
                'limit': 500,
            }
            resp = requests.get(f"{cls.BASE_URL}/query", params=params, timeout=15)
            if resp.status_code != 200:
                return cls._fallback(lat, lng)

            data = resp.json()
            features = data.get('features', [])

            events = []
            for f in features:
                props = f.get('properties', {})
                coords = f.get('geometry', {}).get('coordinates', [0, 0, 0])
                events.append({
                    'id': f.get('id', ''),
                    'magnitude': props.get('mag', 0),
                    'place': props.get('place', ''),
                    'time': datetime.fromtimestamp(props.get('time', 0) / 1000).isoformat() if props.get('time') else '',
                    'depth_km': coords[2] if len(coords) > 2 else 0,
                    'type': props.get('type', 'earthquake'),
                    'lat': coords[1] if len(coords) > 1 else 0,
                    'lng': coords[0],
                    'distance_miles': haversine(lat, lng, coords[1] if len(coords) > 1 else 0, coords[0]),
                })

            return set_cache(cache_key, events)
        except Exception as e:
            logger.warning(f"USGS earthquake API error: {e}")
            return cls._fallback(lat, lng)

    @classmethod
    def calculate_seismic_risk(cls, lat, lng):
        cache_key = f"seismic_risk_{lat:.2f}_{lng:.2f}"
        cached = get_cached(cache_key)
        if cached:
            return cached

        events = cls.get_seismic_history(lat, lng, radius_km=200, years=20)
        if not isinstance(events, list):
            events = []

        total = len(events)
        mag_3_plus = len([e for e in events if e.get('magnitude', 0) >= 3.0])
        mag_4_plus = len([e for e in events if e.get('magnitude', 0) >= 4.0])
        mag_5_plus = len([e for e in events if e.get('magnitude', 0) >= 5.0])
        max_mag = max((e.get('magnitude', 0) for e in events), default=0)

        nearby_50mi = len([e for e in events if e.get('distance_miles', 999) <= 50])
        nearby_25mi = len([e for e in events if e.get('distance_miles', 999) <= 25])

        score = 90

        if mag_5_plus > 0:
            score -= 25
        if mag_4_plus > 5:
            score -= 15
        elif mag_4_plus > 0:
            score -= 8
        if mag_3_plus > 20:
            score -= 15
        elif mag_3_plus > 10:
            score -= 8
        if nearby_25mi > 10:
            score -= 10
        elif nearby_25mi > 5:
            score -= 5
        if max_mag >= 6.0:
            score -= 20
        elif max_mag >= 5.0:
            score -= 10

        score = max(0, min(100, score))

        if score >= 80:
            risk = 'low'
        elif score >= 60:
            risk = 'moderate'
        elif score >= 40:
            risk = 'elevated'
        else:
            risk = 'high'

        result = {
            'seismic_safety_score': score,
            'seismic_risk_level': risk,
            'total_events_20yr': total,
            'magnitude_3_plus': mag_3_plus,
            'magnitude_4_plus': mag_4_plus,
            'magnitude_5_plus': mag_5_plus,
            'max_magnitude': max_mag,
            'events_within_25mi': nearby_25mi,
            'events_within_50mi': nearby_50mi,
            'recent_significant': events[:5] if events else [],
            'assessment': cls._assessment(score, total, max_mag, mag_5_plus),
            'data_source': 'USGS Earthquake Hazards Program',
            'analysis_period': '20 years',
            'search_radius_km': 200,
        }
        return set_cache(cache_key, result)

    @classmethod
    def _assessment(cls, score, total, max_mag, mag_5):
        if score >= 80:
            return f"Low seismic risk. {total} minor events in 20 years, max magnitude {max_mag}. Standard construction practices adequate."
        elif score >= 60:
            return f"Moderate seismic risk. {total} events recorded, max magnitude {max_mag}. Seismic bracing recommended for critical infrastructure."
        elif score >= 40:
            return f"Elevated seismic risk. {total} events including {mag_5} significant (5.0+). Enhanced seismic design required."
        else:
            return f"High seismic risk zone. {total} events, max magnitude {max_mag}. Full seismic engineering and redundancy essential."

    @classmethod
    def _fallback(cls, lat, lng):
        return []


# ─── FEMA NATURAL HAZARD RISK ────────────────────────────────────────────────

class FEMAHazardAPI:
    HAZARD_DATA = {
        'VA': {'overall': 72, 'flood': 65, 'tornado': 35, 'hurricane': 60, 'earthquake': 25, 'wildfire': 20, 'winter_storm': 55, 'hail': 40},
        'TX': {'overall': 55, 'flood': 70, 'tornado': 75, 'hurricane': 65, 'earthquake': 15, 'wildfire': 50, 'winter_storm': 45, 'hail': 70},
        'AZ': {'overall': 78, 'flood': 30, 'tornado': 10, 'hurricane': 5, 'earthquake': 30, 'wildfire': 45, 'winter_storm': 10, 'hail': 25},
        'GA': {'overall': 65, 'flood': 55, 'tornado': 55, 'hurricane': 50, 'earthquake': 15, 'wildfire': 25, 'winter_storm': 35, 'hail': 45},
        'OH': {'overall': 70, 'flood': 50, 'tornado': 55, 'hurricane': 10, 'earthquake': 20, 'wildfire': 10, 'winter_storm': 65, 'hail': 50},
        'IL': {'overall': 65, 'flood': 55, 'tornado': 65, 'hurricane': 5, 'earthquake': 30, 'wildfire': 10, 'winter_storm': 60, 'hail': 55},
        'CA': {'overall': 50, 'flood': 45, 'tornado': 5, 'hurricane': 5, 'earthquake': 85, 'wildfire': 75, 'winter_storm': 15, 'hail': 10},
        'WA': {'overall': 68, 'flood': 50, 'tornado': 10, 'hurricane': 5, 'earthquake': 65, 'wildfire': 45, 'winter_storm': 40, 'hail': 15},
        'OR': {'overall': 70, 'flood': 45, 'tornado': 10, 'hurricane': 5, 'earthquake': 55, 'wildfire': 50, 'winter_storm': 35, 'hail': 15},
        'NV': {'overall': 80, 'flood': 25, 'tornado': 5, 'hurricane': 5, 'earthquake': 40, 'wildfire': 30, 'winter_storm': 20, 'hail': 15},
        'UT': {'overall': 75, 'flood': 30, 'tornado': 10, 'hurricane': 5, 'earthquake': 45, 'wildfire': 35, 'winter_storm': 40, 'hail': 20},
        'CO': {'overall': 72, 'flood': 35, 'tornado': 45, 'hurricane': 5, 'earthquake': 15, 'wildfire': 45, 'winter_storm': 50, 'hail': 60},
        'NC': {'overall': 60, 'flood': 60, 'tornado': 45, 'hurricane': 65, 'earthquake': 20, 'wildfire': 25, 'winter_storm': 40, 'hail': 40},
        'SC': {'overall': 58, 'flood': 60, 'tornado': 40, 'hurricane': 65, 'earthquake': 30, 'wildfire': 25, 'winter_storm': 30, 'hail': 35},
        'NJ': {'overall': 62, 'flood': 65, 'tornado': 25, 'hurricane': 55, 'earthquake': 15, 'wildfire': 15, 'winter_storm': 60, 'hail': 30},
        'NY': {'overall': 64, 'flood': 60, 'tornado': 25, 'hurricane': 45, 'earthquake': 20, 'wildfire': 10, 'winter_storm': 65, 'hail': 30},
        'PA': {'overall': 68, 'flood': 55, 'tornado': 30, 'hurricane': 30, 'earthquake': 15, 'wildfire': 10, 'winter_storm': 60, 'hail': 35},
        'IA': {'overall': 62, 'flood': 60, 'tornado': 70, 'hurricane': 5, 'earthquake': 10, 'wildfire': 10, 'winter_storm': 65, 'hail': 65},
        'NE': {'overall': 60, 'flood': 45, 'tornado': 75, 'hurricane': 5, 'earthquake': 10, 'wildfire': 20, 'winter_storm': 60, 'hail': 70},
        'MN': {'overall': 68, 'flood': 50, 'tornado': 55, 'hurricane': 5, 'earthquake': 5, 'wildfire': 15, 'winter_storm': 70, 'hail': 55},
        'WI': {'overall': 70, 'flood': 45, 'tornado': 45, 'hurricane': 5, 'earthquake': 10, 'wildfire': 15, 'winter_storm': 65, 'hail': 45},
        'MI': {'overall': 72, 'flood': 45, 'tornado': 40, 'hurricane': 5, 'earthquake': 10, 'wildfire': 15, 'winter_storm': 60, 'hail': 40},
        'IN': {'overall': 66, 'flood': 50, 'tornado': 60, 'hurricane': 5, 'earthquake': 25, 'wildfire': 10, 'winter_storm': 55, 'hail': 50},
        'MO': {'overall': 60, 'flood': 55, 'tornado': 65, 'hurricane': 5, 'earthquake': 40, 'wildfire': 15, 'winter_storm': 50, 'hail': 55},
        'TN': {'overall': 62, 'flood': 55, 'tornado': 55, 'hurricane': 15, 'earthquake': 35, 'wildfire': 15, 'winter_storm': 35, 'hail': 45},
        'AL': {'overall': 55, 'flood': 55, 'tornado': 70, 'hurricane': 60, 'earthquake': 15, 'wildfire': 20, 'winter_storm': 25, 'hail': 50},
        'MS': {'overall': 52, 'flood': 60, 'tornado': 65, 'hurricane': 65, 'earthquake': 15, 'wildfire': 20, 'winter_storm': 20, 'hail': 45},
        'LA': {'overall': 45, 'flood': 75, 'tornado': 50, 'hurricane': 80, 'earthquake': 10, 'wildfire': 15, 'winter_storm': 15, 'hail': 40},
        'FL': {'overall': 48, 'flood': 70, 'tornado': 40, 'hurricane': 85, 'earthquake': 5, 'wildfire': 35, 'winter_storm': 5, 'hail': 30},
        'ID': {'overall': 76, 'flood': 30, 'tornado': 10, 'hurricane': 5, 'earthquake': 40, 'wildfire': 45, 'winter_storm': 45, 'hail': 15},
        'MT': {'overall': 78, 'flood': 30, 'tornado': 15, 'hurricane': 5, 'earthquake': 35, 'wildfire': 40, 'winter_storm': 55, 'hail': 25},
        'WY': {'overall': 80, 'flood': 25, 'tornado': 20, 'hurricane': 5, 'earthquake': 25, 'wildfire': 30, 'winter_storm': 55, 'hail': 30},
        'ND': {'overall': 72, 'flood': 45, 'tornado': 50, 'hurricane': 5, 'earthquake': 5, 'wildfire': 15, 'winter_storm': 70, 'hail': 55},
        'SD': {'overall': 68, 'flood': 40, 'tornado': 55, 'hurricane': 5, 'earthquake': 5, 'wildfire': 20, 'winter_storm': 65, 'hail': 60},
        'KS': {'overall': 58, 'flood': 45, 'tornado': 80, 'hurricane': 5, 'earthquake': 20, 'wildfire': 25, 'winter_storm': 50, 'hail': 75},
        'OK': {'overall': 52, 'flood': 50, 'tornado': 85, 'hurricane': 10, 'earthquake': 45, 'wildfire': 30, 'winter_storm': 45, 'hail': 75},
        'AR': {'overall': 58, 'flood': 55, 'tornado': 65, 'hurricane': 20, 'earthquake': 30, 'wildfire': 20, 'winter_storm': 35, 'hail': 55},
        'NM': {'overall': 78, 'flood': 30, 'tornado': 15, 'hurricane': 5, 'earthquake': 20, 'wildfire': 40, 'winter_storm': 25, 'hail': 30},
        'CT': {'overall': 68, 'flood': 55, 'tornado': 20, 'hurricane': 45, 'earthquake': 15, 'wildfire': 10, 'winter_storm': 60, 'hail': 25},
        'MA': {'overall': 66, 'flood': 55, 'tornado': 20, 'hurricane': 45, 'earthquake': 15, 'wildfire': 10, 'winter_storm': 65, 'hail': 25},
        'MD': {'overall': 64, 'flood': 55, 'tornado': 30, 'hurricane': 50, 'earthquake': 15, 'wildfire': 15, 'winter_storm': 55, 'hail': 35},
        'DE': {'overall': 64, 'flood': 55, 'tornado': 25, 'hurricane': 50, 'earthquake': 10, 'wildfire': 10, 'winter_storm': 50, 'hail': 30},
        'NH': {'overall': 74, 'flood': 45, 'tornado': 15, 'hurricane': 30, 'earthquake': 15, 'wildfire': 10, 'winter_storm': 65, 'hail': 20},
        'VT': {'overall': 74, 'flood': 45, 'tornado': 15, 'hurricane': 25, 'earthquake': 15, 'wildfire': 10, 'winter_storm': 65, 'hail': 20},
        'ME': {'overall': 76, 'flood': 40, 'tornado': 10, 'hurricane': 25, 'earthquake': 10, 'wildfire': 15, 'winter_storm': 65, 'hail': 15},
        'RI': {'overall': 66, 'flood': 55, 'tornado': 15, 'hurricane': 50, 'earthquake': 15, 'wildfire': 10, 'winter_storm': 60, 'hail': 20},
        'WV': {'overall': 72, 'flood': 55, 'tornado': 25, 'hurricane': 15, 'earthquake': 15, 'wildfire': 10, 'winter_storm': 55, 'hail': 30},
        'KY': {'overall': 65, 'flood': 55, 'tornado': 50, 'hurricane': 10, 'earthquake': 30, 'wildfire': 15, 'winter_storm': 45, 'hail': 45},
        'HI': {'overall': 55, 'flood': 50, 'tornado': 5, 'hurricane': 60, 'earthquake': 55, 'wildfire': 30, 'winter_storm': 5, 'hail': 5},
        'AK': {'overall': 60, 'flood': 35, 'tornado': 5, 'hurricane': 5, 'earthquake': 80, 'wildfire': 40, 'winter_storm': 70, 'hail': 10},
    }

    @classmethod
    def get_hazard_risk(cls, lat, lng, state=None):
        if not state:
            state = cls._detect_state(lat, lng)

        state = state.upper() if state else 'VA'
        data = cls.HAZARD_DATA.get(state, cls.HAZARD_DATA.get('VA'))

        hazards = []
        for hazard_type in ['flood', 'tornado', 'hurricane', 'earthquake', 'wildfire', 'winter_storm', 'hail']:
            risk_val = data.get(hazard_type, 30)
            if risk_val >= 70:
                level = 'high'
            elif risk_val >= 50:
                level = 'moderate'
            elif risk_val >= 30:
                level = 'low'
            else:
                level = 'very_low'

            hazards.append({
                'type': hazard_type.replace('_', ' ').title(),
                'risk_score': risk_val,
                'risk_level': level,
            })

        hazards.sort(key=lambda x: x['risk_score'], reverse=True)
        top_risks = [h['type'] for h in hazards if h['risk_level'] in ('high', 'moderate')]

        return {
            'natural_hazard_safety_score': data['overall'],
            'state': state,
            'hazards': hazards,
            'top_risks': top_risks[:3],
            'assessment': cls._assessment(data['overall'], top_risks),
            'data_source': 'FEMA National Risk Index (modeled)',
        }

    @classmethod
    def _assessment(cls, score, top_risks):
        risks_str = ', '.join(top_risks[:3]) if top_risks else 'none significant'
        if score >= 75:
            return f"Low overall natural hazard risk. Top concerns: {risks_str}. Standard resilience measures sufficient."
        elif score >= 60:
            return f"Moderate natural hazard risk. Key risks: {risks_str}. Enhanced monitoring and mitigation recommended."
        elif score >= 45:
            return f"Elevated natural hazard risk. Significant risks: {risks_str}. Comprehensive disaster recovery planning essential."
        else:
            return f"High natural hazard exposure. Major risks: {risks_str}. Full redundancy and hardened facility design required."

    @classmethod
    def _detect_state(cls, lat, lng):
        state_centers = {
            'AL': (32.8, -86.8), 'AK': (64.0, -153.0), 'AZ': (34.3, -111.7),
            'AR': (34.9, -92.4), 'CA': (37.2, -119.5), 'CO': (39.0, -105.5),
            'CT': (41.6, -72.7), 'DE': (39.0, -75.5), 'FL': (28.6, -82.5),
            'GA': (32.7, -83.4), 'HI': (20.5, -157.5), 'ID': (44.4, -114.6),
            'IL': (40.0, -89.2), 'IN': (39.9, -86.3), 'IA': (42.0, -93.5),
            'KS': (38.5, -98.3), 'KY': (37.8, -85.7), 'LA': (31.1, -92.0),
            'ME': (45.4, -69.2), 'MD': (39.1, -76.8), 'MA': (42.2, -71.5),
            'MI': (44.2, -84.5), 'MN': (46.3, -94.3), 'MS': (32.7, -89.7),
            'MO': (38.5, -92.3), 'MT': (47.0, -109.6), 'NE': (41.5, -99.8),
            'NV': (39.3, -116.6), 'NH': (43.7, -71.6), 'NJ': (40.2, -74.7),
            'NM': (34.5, -106.1), 'NY': (42.9, -75.5), 'NC': (35.5, -79.8),
            'ND': (47.4, -100.5), 'OH': (40.4, -82.8), 'OK': (35.6, -97.5),
            'OR': (44.0, -120.5), 'PA': (40.9, -77.8), 'RI': (41.7, -71.5),
            'SC': (33.9, -80.9), 'SD': (44.4, -100.2), 'TN': (35.9, -86.4),
            'TX': (31.5, -99.3), 'UT': (39.3, -111.7), 'VT': (44.1, -72.6),
            'VA': (37.5, -78.9), 'WA': (47.4, -120.5), 'WV': (38.6, -80.6),
            'WI': (44.6, -89.8), 'WY': (43.0, -107.5),
        }
        best = 'VA'
        best_dist = 9999
        for st, (slat, slng) in state_centers.items():
            d = haversine(lat, lng, slat, slng)
            if d < best_dist:
                best_dist = d
                best = st
        return best


# ─── NOAA CLIMATE DATA ───────────────────────────────────────────────────────

class NOAAClimateAPI:
    STATE_CLIMATE = {
        'VA': {'avg_temp_f': 56, 'avg_high_f': 67, 'avg_low_f': 45, 'annual_precip_in': 44, 'cooling_degree_days': 1200, 'humidity_pct': 65, 'avg_wind_mph': 8, 'climate_zone': '4A - Mixed Humid'},
        'TX': {'avg_temp_f': 65, 'avg_high_f': 78, 'avg_low_f': 53, 'annual_precip_in': 35, 'cooling_degree_days': 2800, 'humidity_pct': 60, 'avg_wind_mph': 11, 'climate_zone': '2A/3A - Hot Humid'},
        'AZ': {'avg_temp_f': 62, 'avg_high_f': 80, 'avg_low_f': 48, 'annual_precip_in': 13, 'cooling_degree_days': 3200, 'humidity_pct': 25, 'avg_wind_mph': 7, 'climate_zone': '2B - Hot Dry'},
        'GA': {'avg_temp_f': 62, 'avg_high_f': 73, 'avg_low_f': 50, 'annual_precip_in': 50, 'cooling_degree_days': 1900, 'humidity_pct': 70, 'avg_wind_mph': 8, 'climate_zone': '3A - Warm Humid'},
        'OH': {'avg_temp_f': 51, 'avg_high_f': 61, 'avg_low_f': 41, 'annual_precip_in': 39, 'cooling_degree_days': 800, 'humidity_pct': 68, 'avg_wind_mph': 9, 'climate_zone': '5A - Cool Humid'},
        'IL': {'avg_temp_f': 52, 'avg_high_f': 62, 'avg_low_f': 42, 'annual_precip_in': 38, 'cooling_degree_days': 900, 'humidity_pct': 67, 'avg_wind_mph': 10, 'climate_zone': '5A - Cool Humid'},
        'CA': {'avg_temp_f': 60, 'avg_high_f': 73, 'avg_low_f': 48, 'annual_precip_in': 22, 'cooling_degree_days': 1100, 'humidity_pct': 45, 'avg_wind_mph': 7, 'climate_zone': '3B/3C - Warm Marine'},
        'WA': {'avg_temp_f': 48, 'avg_high_f': 58, 'avg_low_f': 40, 'annual_precip_in': 38, 'cooling_degree_days': 300, 'humidity_pct': 70, 'avg_wind_mph': 8, 'climate_zone': '4C - Mixed Marine'},
        'OR': {'avg_temp_f': 50, 'avg_high_f': 60, 'avg_low_f': 40, 'annual_precip_in': 36, 'cooling_degree_days': 350, 'humidity_pct': 68, 'avg_wind_mph': 7, 'climate_zone': '4C - Mixed Marine'},
        'NV': {'avg_temp_f': 55, 'avg_high_f': 72, 'avg_low_f': 38, 'annual_precip_in': 10, 'cooling_degree_days': 2200, 'humidity_pct': 22, 'avg_wind_mph': 8, 'climate_zone': '3B/5B - Dry'},
        'UT': {'avg_temp_f': 50, 'avg_high_f': 63, 'avg_low_f': 37, 'annual_precip_in': 16, 'cooling_degree_days': 1100, 'humidity_pct': 30, 'avg_wind_mph': 8, 'climate_zone': '5B - Cool Dry'},
        'CO': {'avg_temp_f': 46, 'avg_high_f': 60, 'avg_low_f': 32, 'annual_precip_in': 17, 'cooling_degree_days': 600, 'humidity_pct': 35, 'avg_wind_mph': 9, 'climate_zone': '5B - Cool Dry'},
        'NC': {'avg_temp_f': 59, 'avg_high_f': 70, 'avg_low_f': 48, 'annual_precip_in': 48, 'cooling_degree_days': 1500, 'humidity_pct': 70, 'avg_wind_mph': 7, 'climate_zone': '3A/4A - Mixed Humid'},
        'NJ': {'avg_temp_f': 53, 'avg_high_f': 63, 'avg_low_f': 43, 'annual_precip_in': 47, 'cooling_degree_days': 900, 'humidity_pct': 65, 'avg_wind_mph': 9, 'climate_zone': '4A - Mixed Humid'},
        'NY': {'avg_temp_f': 48, 'avg_high_f': 58, 'avg_low_f': 38, 'annual_precip_in': 46, 'cooling_degree_days': 700, 'humidity_pct': 66, 'avg_wind_mph': 9, 'climate_zone': '4A/5A - Mixed/Cool Humid'},
        'PA': {'avg_temp_f': 50, 'avg_high_f': 60, 'avg_low_f': 40, 'annual_precip_in': 43, 'cooling_degree_days': 750, 'humidity_pct': 66, 'avg_wind_mph': 8, 'climate_zone': '4A/5A - Mixed/Cool Humid'},
        'FL': {'avg_temp_f': 72, 'avg_high_f': 82, 'avg_low_f': 62, 'annual_precip_in': 54, 'cooling_degree_days': 3500, 'humidity_pct': 75, 'avg_wind_mph': 9, 'climate_zone': '1A/2A - Very Hot Humid'},
        'SC': {'avg_temp_f': 62, 'avg_high_f': 73, 'avg_low_f': 50, 'annual_precip_in': 48, 'cooling_degree_days': 1800, 'humidity_pct': 70, 'avg_wind_mph': 7, 'climate_zone': '3A - Warm Humid'},
        'MD': {'avg_temp_f': 55, 'avg_high_f': 65, 'avg_low_f': 44, 'annual_precip_in': 43, 'cooling_degree_days': 1100, 'humidity_pct': 65, 'avg_wind_mph': 8, 'climate_zone': '4A - Mixed Humid'},
        'IA': {'avg_temp_f': 48, 'avg_high_f': 59, 'avg_low_f': 37, 'annual_precip_in': 35, 'cooling_degree_days': 800, 'humidity_pct': 65, 'avg_wind_mph': 11, 'climate_zone': '5A - Cool Humid'},
        'MN': {'avg_temp_f': 42, 'avg_high_f': 53, 'avg_low_f': 31, 'annual_precip_in': 30, 'cooling_degree_days': 500, 'humidity_pct': 65, 'avg_wind_mph': 10, 'climate_zone': '6A - Cold Humid'},
        'WI': {'avg_temp_f': 44, 'avg_high_f': 55, 'avg_low_f': 33, 'annual_precip_in': 33, 'cooling_degree_days': 500, 'humidity_pct': 67, 'avg_wind_mph': 10, 'climate_zone': '6A - Cold Humid'},
        'MI': {'avg_temp_f': 46, 'avg_high_f': 56, 'avg_low_f': 36, 'annual_precip_in': 33, 'cooling_degree_days': 550, 'humidity_pct': 68, 'avg_wind_mph': 10, 'climate_zone': '5A/6A - Cool/Cold Humid'},
        'IN': {'avg_temp_f': 52, 'avg_high_f': 62, 'avg_low_f': 42, 'annual_precip_in': 42, 'cooling_degree_days': 900, 'humidity_pct': 68, 'avg_wind_mph': 9, 'climate_zone': '4A/5A - Mixed/Cool Humid'},
        'MO': {'avg_temp_f': 55, 'avg_high_f': 66, 'avg_low_f': 44, 'annual_precip_in': 42, 'cooling_degree_days': 1200, 'humidity_pct': 66, 'avg_wind_mph': 10, 'climate_zone': '4A - Mixed Humid'},
        'TN': {'avg_temp_f': 58, 'avg_high_f': 69, 'avg_low_f': 47, 'annual_precip_in': 52, 'cooling_degree_days': 1400, 'humidity_pct': 68, 'avg_wind_mph': 7, 'climate_zone': '4A - Mixed Humid'},
        'AL': {'avg_temp_f': 63, 'avg_high_f': 73, 'avg_low_f': 52, 'annual_precip_in': 56, 'cooling_degree_days': 2000, 'humidity_pct': 72, 'avg_wind_mph': 7, 'climate_zone': '3A - Warm Humid'},
        'LA': {'avg_temp_f': 67, 'avg_high_f': 77, 'avg_low_f': 56, 'annual_precip_in': 60, 'cooling_degree_days': 2600, 'humidity_pct': 75, 'avg_wind_mph': 8, 'climate_zone': '2A - Hot Humid'},
        'MS': {'avg_temp_f': 63, 'avg_high_f': 74, 'avg_low_f': 53, 'annual_precip_in': 56, 'cooling_degree_days': 2100, 'humidity_pct': 73, 'avg_wind_mph': 7, 'climate_zone': '3A - Warm Humid'},
        'KS': {'avg_temp_f': 55, 'avg_high_f': 67, 'avg_low_f': 42, 'annual_precip_in': 33, 'cooling_degree_days': 1200, 'humidity_pct': 58, 'avg_wind_mph': 12, 'climate_zone': '4A - Mixed Humid'},
        'OK': {'avg_temp_f': 60, 'avg_high_f': 72, 'avg_low_f': 48, 'annual_precip_in': 37, 'cooling_degree_days': 1800, 'humidity_pct': 60, 'avg_wind_mph': 11, 'climate_zone': '3A - Warm Humid'},
        'NE': {'avg_temp_f': 49, 'avg_high_f': 61, 'avg_low_f': 37, 'annual_precip_in': 27, 'cooling_degree_days': 900, 'humidity_pct': 60, 'avg_wind_mph': 11, 'climate_zone': '5A - Cool Humid'},
        'ID': {'avg_temp_f': 45, 'avg_high_f': 58, 'avg_low_f': 32, 'annual_precip_in': 19, 'cooling_degree_days': 500, 'humidity_pct': 40, 'avg_wind_mph': 7, 'climate_zone': '5B/6B - Cool/Cold Dry'},
        'MT': {'avg_temp_f': 43, 'avg_high_f': 56, 'avg_low_f': 30, 'annual_precip_in': 15, 'cooling_degree_days': 300, 'humidity_pct': 45, 'avg_wind_mph': 10, 'climate_zone': '6B - Cold Dry'},
        'WY': {'avg_temp_f': 42, 'avg_high_f': 56, 'avg_low_f': 28, 'annual_precip_in': 13, 'cooling_degree_days': 250, 'humidity_pct': 40, 'avg_wind_mph': 12, 'climate_zone': '6B - Cold Dry'},
        'ND': {'avg_temp_f': 40, 'avg_high_f': 53, 'avg_low_f': 27, 'annual_precip_in': 18, 'cooling_degree_days': 400, 'humidity_pct': 62, 'avg_wind_mph': 11, 'climate_zone': '6A/7 - Cold'},
        'SD': {'avg_temp_f': 46, 'avg_high_f': 59, 'avg_low_f': 33, 'annual_precip_in': 22, 'cooling_degree_days': 600, 'humidity_pct': 58, 'avg_wind_mph': 11, 'climate_zone': '5A/6A - Cool/Cold'},
        'NM': {'avg_temp_f': 53, 'avg_high_f': 68, 'avg_low_f': 38, 'annual_precip_in': 14, 'cooling_degree_days': 1200, 'humidity_pct': 28, 'avg_wind_mph': 8, 'climate_zone': '4B/5B - Mixed/Cool Dry'},
        'AR': {'avg_temp_f': 60, 'avg_high_f': 71, 'avg_low_f': 49, 'annual_precip_in': 50, 'cooling_degree_days': 1700, 'humidity_pct': 68, 'avg_wind_mph': 8, 'climate_zone': '3A - Warm Humid'},
        'CT': {'avg_temp_f': 50, 'avg_high_f': 59, 'avg_low_f': 40, 'annual_precip_in': 48, 'cooling_degree_days': 600, 'humidity_pct': 65, 'avg_wind_mph': 8, 'climate_zone': '5A - Cool Humid'},
        'MA': {'avg_temp_f': 49, 'avg_high_f': 58, 'avg_low_f': 39, 'annual_precip_in': 47, 'cooling_degree_days': 550, 'humidity_pct': 65, 'avg_wind_mph': 10, 'climate_zone': '5A - Cool Humid'},
        'NH': {'avg_temp_f': 44, 'avg_high_f': 55, 'avg_low_f': 34, 'annual_precip_in': 44, 'cooling_degree_days': 400, 'humidity_pct': 65, 'avg_wind_mph': 7, 'climate_zone': '5A/6A - Cool/Cold Humid'},
        'VT': {'avg_temp_f': 43, 'avg_high_f': 54, 'avg_low_f': 32, 'annual_precip_in': 40, 'cooling_degree_days': 350, 'humidity_pct': 66, 'avg_wind_mph': 7, 'climate_zone': '6A - Cold Humid'},
        'ME': {'avg_temp_f': 41, 'avg_high_f': 52, 'avg_low_f': 30, 'annual_precip_in': 45, 'cooling_degree_days': 250, 'humidity_pct': 68, 'avg_wind_mph': 8, 'climate_zone': '6A/7 - Cold'},
        'RI': {'avg_temp_f': 50, 'avg_high_f': 59, 'avg_low_f': 40, 'annual_precip_in': 47, 'cooling_degree_days': 550, 'humidity_pct': 65, 'avg_wind_mph': 10, 'climate_zone': '5A - Cool Humid'},
        'WV': {'avg_temp_f': 52, 'avg_high_f': 62, 'avg_low_f': 42, 'annual_precip_in': 44, 'cooling_degree_days': 700, 'humidity_pct': 68, 'avg_wind_mph': 7, 'climate_zone': '4A/5A - Mixed/Cool Humid'},
        'KY': {'avg_temp_f': 56, 'avg_high_f': 66, 'avg_low_f': 45, 'annual_precip_in': 48, 'cooling_degree_days': 1100, 'humidity_pct': 68, 'avg_wind_mph': 8, 'climate_zone': '4A - Mixed Humid'},
        'HI': {'avg_temp_f': 75, 'avg_high_f': 83, 'avg_low_f': 68, 'annual_precip_in': 25, 'cooling_degree_days': 4500, 'humidity_pct': 70, 'avg_wind_mph': 12, 'climate_zone': '1A - Very Hot Humid'},
        'AK': {'avg_temp_f': 27, 'avg_high_f': 37, 'avg_low_f': 17, 'annual_precip_in': 22, 'cooling_degree_days': 0, 'humidity_pct': 65, 'avg_wind_mph': 7, 'climate_zone': '7/8 - Subarctic'},
        'DE': {'avg_temp_f': 55, 'avg_high_f': 64, 'avg_low_f': 44, 'annual_precip_in': 45, 'cooling_degree_days': 1000, 'humidity_pct': 66, 'avg_wind_mph': 9, 'climate_zone': '4A - Mixed Humid'},
    }

    @classmethod
    def get_climate_data(cls, lat, lng, state=None):
        if not state:
            state = FEMAHazardAPI._detect_state(lat, lng)

        state = state.upper() if state else 'VA'
        data = cls.STATE_CLIMATE.get(state, cls.STATE_CLIMATE.get('VA'))

        cdd = data['cooling_degree_days']
        if cdd < 500:
            cooling_favorability = 'excellent'
            cooling_score = 95
        elif cdd < 1000:
            cooling_favorability = 'good'
            cooling_score = 80
        elif cdd < 2000:
            cooling_favorability = 'moderate'
            cooling_score = 60
        elif cdd < 3000:
            cooling_favorability = 'challenging'
            cooling_score = 40
        else:
            cooling_favorability = 'poor'
            cooling_score = 20

        humidity = data['humidity_pct']
        if humidity < 35:
            free_cooling_hours = 6500
        elif humidity < 50:
            free_cooling_hours = 5500
        elif humidity < 65:
            free_cooling_hours = 4000
        else:
            free_cooling_hours = 2500

        return {
            'climate_zone': data['climate_zone'],
            'avg_temperature_f': data['avg_temp_f'],
            'avg_high_f': data['avg_high_f'],
            'avg_low_f': data['avg_low_f'],
            'annual_precipitation_in': data['annual_precip_in'],
            'cooling_degree_days': cdd,
            'average_humidity_pct': humidity,
            'average_wind_mph': data['avg_wind_mph'],
            'cooling_favorability': cooling_favorability,
            'cooling_efficiency_score': cooling_score,
            'estimated_free_cooling_hours': free_cooling_hours,
            'pue_estimate': cls._estimate_pue(cdd, humidity),
            'dc_climate_assessment': cls._assessment(cooling_score, cdd, humidity, free_cooling_hours),
            'state': state,
            'data_source': 'NOAA Climate Normals (modeled from 30-year averages)',
        }

    @classmethod
    def _estimate_pue(cls, cdd, humidity):
        base_pue = 1.20
        if cdd > 2500:
            base_pue += 0.25
        elif cdd > 1500:
            base_pue += 0.15
        elif cdd > 800:
            base_pue += 0.05

        if humidity > 70:
            base_pue += 0.08
        elif humidity > 55:
            base_pue += 0.03

        return round(base_pue, 2)

    @classmethod
    def _assessment(cls, score, cdd, humidity, free_cooling):
        if score >= 80:
            return f"Excellent climate for data centers. {cdd} CDD, {humidity}% avg humidity. ~{free_cooling:,} free cooling hours/year. Low PUE achievable."
        elif score >= 60:
            return f"Moderate climate. {cdd} CDD, {humidity}% humidity. ~{free_cooling:,} free cooling hours. Standard HVAC with economizers recommended."
        elif score >= 40:
            return f"Challenging climate. {cdd} CDD, {humidity}% humidity. Limited free cooling (~{free_cooling:,} hrs). High-efficiency cooling systems required."
        else:
            return f"Difficult climate for cooling. {cdd} CDD, {humidity}% humidity. Minimal free cooling. Advanced cooling (liquid, immersion) strongly recommended."


# ─── COMPOSITE DC SITE RISK SCORE ────────────────────────────────────────────

def _fetch_water(lat, lng, state=None):
    try:
        return {'status': 'ok', 'data': USGSWaterAPI.calculate_water_stress(lat, lng)}
    except Exception as e:
        logger.warning(f"Water API failed for {lat},{lng}: {e}")
        return {'status': 'timeout', 'data': {}, 'message': f'USGS Water API unavailable: {str(e)}'}

def _fetch_seismic(lat, lng, state=None):
    try:
        return {'status': 'ok', 'data': USGSEarthquakeAPI.calculate_seismic_risk(lat, lng)}
    except Exception as e:
        logger.warning(f"Seismic API failed for {lat},{lng}: {e}")
        return {'status': 'timeout', 'data': {}, 'message': f'USGS Earthquake API unavailable: {str(e)}'}

def _fetch_hazard(lat, lng, state=None):
    try:
        return {'status': 'ok', 'data': FEMAHazardAPI.get_hazard_risk(lat, lng, state)}
    except Exception as e:
        logger.warning(f"FEMA API failed for {lat},{lng}: {e}")
        return {'status': 'timeout', 'data': {}, 'message': f'FEMA API unavailable: {str(e)}'}

def _fetch_climate(lat, lng, state=None):
    try:
        return {'status': 'ok', 'data': NOAAClimateAPI.get_climate_data(lat, lng, state)}
    except Exception as e:
        logger.warning(f"NOAA API failed for {lat},{lng}: {e}")
        return {'status': 'timeout', 'data': {}, 'message': f'NOAA API unavailable: {str(e)}'}


def calculate_composite_risk(lat, lng, state=None):
    cache_key = f"composite_{lat:.2f}_{lng:.2f}"
    cached = get_cached(cache_key, ttl=COMPOSITE_CACHE_DURATION)
    if cached:
        return cached

    with ThreadPoolExecutor(max_workers=4) as executor:
        water_future = executor.submit(_fetch_water, lat, lng, state)
        seismic_future = executor.submit(_fetch_seismic, lat, lng, state)
        hazard_future = executor.submit(_fetch_hazard, lat, lng, state)
        climate_future = executor.submit(_fetch_climate, lat, lng, state)

        water_result = water_future.result(timeout=20)
        seismic_result = seismic_future.result(timeout=20)
        hazard_result = hazard_future.result(timeout=20)
        climate_result = climate_future.result(timeout=20)

    water = water_result['data']
    seismic = seismic_result['data']
    hazard = hazard_result['data']
    climate = climate_result['data']

    partial = any(r['status'] != 'ok' for r in [water_result, seismic_result, hazard_result, climate_result])

    water_score = water.get('water_availability_score', 50)
    seismic_score = seismic.get('seismic_safety_score', 70)
    hazard_score = hazard.get('natural_hazard_safety_score', 60)
    climate_score = climate.get('cooling_efficiency_score', 60)

    composite = round(
        water_score * 0.30 +
        seismic_score * 0.25 +
        hazard_score * 0.25 +
        climate_score * 0.20
    )

    if composite >= 85:
        grade = 'A'
    elif composite >= 75:
        grade = 'B'
    elif composite >= 60:
        grade = 'C'
    elif composite >= 45:
        grade = 'D'
    else:
        grade = 'F'

    factors = [
        {'name': 'Water Availability', 'score': water_score if water_result['status'] == 'ok' else None, 'weight': '30%', 'risk': water.get('water_stress_risk', 'unknown'), 'status': water_result['status']},
        {'name': 'Seismic Safety', 'score': seismic_score if seismic_result['status'] == 'ok' else None, 'weight': '25%', 'risk': seismic.get('seismic_risk_level', 'unknown'), 'status': seismic_result['status']},
        {'name': 'Natural Hazard Safety', 'score': hazard_score if hazard_result['status'] == 'ok' else None, 'weight': '25%', 'risk': 'low' if hazard_score >= 70 else 'moderate' if hazard_score >= 50 else 'high', 'status': hazard_result['status']},
        {'name': 'Climate/Cooling', 'score': climate_score if climate_result['status'] == 'ok' else None, 'weight': '20%', 'risk': climate.get('cooling_favorability', 'unknown'), 'status': climate_result['status']},
    ]
    valid_factors = [f for f in factors if f['score'] is not None]
    valid_factors.sort(key=lambda x: x['score'])

    weakest = valid_factors[0] if valid_factors else {'name': 'Unknown', 'score': 0}
    strongest = valid_factors[-1] if valid_factors else {'name': 'Unknown', 'score': 0}

    data_sources = {
        'water': {'status': water_result['status'], 'score': water_score if water_result['status'] == 'ok' else None},
        'seismic': {'status': seismic_result['status'], 'score': seismic_score if seismic_result['status'] == 'ok' else None},
        'hazard': {'status': hazard_result['status'], 'score': hazard_score if hazard_result['status'] == 'ok' else None},
        'climate': {'status': climate_result['status'], 'score': climate_score if climate_result['status'] == 'ok' else None},
    }
    for key, r in [('water', water_result), ('seismic', seismic_result), ('hazard', hazard_result), ('climate', climate_result)]:
        if r['status'] != 'ok':
            data_sources[key]['message'] = r.get('message', 'API unavailable')

    result = {
        'composite_site_risk_score': composite,
        'site_grade': grade,
        'partial': partial,
        'data_sources': data_sources,
        'factors': factors,
        'weakest_factor': weakest['name'],
        'strongest_factor': strongest['name'],
        'water': water,
        'seismic': seismic,
        'natural_hazards': hazard,
        'climate': climate,
        'recommendation': _composite_recommendation(composite, grade, weakest, strongest),
        'competitive_advantage': _competitive_note(composite),
        'coordinates': {'lat': lat, 'lng': lng},
        'generated_at': datetime.utcnow().isoformat(),
    }
    return set_cache(cache_key, result)


def _composite_recommendation(composite, grade, weakest, strongest):
    if composite >= 80:
        return f"Grade {grade} site. Strong across all factors. Best-in-class: {strongest['name']} ({strongest['score']}). This location is well-suited for data center development."
    elif composite >= 65:
        return f"Grade {grade} site. Adequate overall. Consider mitigation for {weakest['name']} ({weakest['score']}). Leverage strength in {strongest['name']} ({strongest['score']})."
    elif composite >= 50:
        return f"Grade {grade} site. Notable risk in {weakest['name']} ({weakest['score']}). Significant investment in mitigation required. May not be optimal for Tier III+ facilities."
    else:
        return f"Grade {grade} site. High risk. {weakest['name']} is critically low ({weakest['score']}). Alternative locations should be strongly considered."


def _competitive_note(composite):
    if composite >= 80:
        return "This site scores in the top tier nationally. Competitive for hyperscale and enterprise deployments."
    elif composite >= 65:
        return "Above-average site. Suitable for most data center types with appropriate engineering."
    elif composite >= 50:
        return "Below-average risk profile. May limit tenant/customer attraction without significant hardening."
    else:
        return "High-risk location. Most institutional investors and hyperscalers would pass on this site."


# ─── FLASK ROUTES ─────────────────────────────────────────────────────────────

@site_risk_bp.route('/api/v1/risk/water', methods=['GET'])
def water_risk():
    lat = request.args.get('lat', type=float)
    lng = request.args.get('lng', type=float)
    if lat is None or lng is None:
        return jsonify({'success': False, 'error': 'lat and lng required'}), 400

    radius = request.args.get('radius', 30, type=int)
    water_stress = USGSWaterAPI.calculate_water_stress(lat, lng)

    return jsonify({
        'success': True,
        'coordinates': {'lat': lat, 'lng': lng},
        **water_stress,
    })


@site_risk_bp.route('/api/v1/risk/water/levels', methods=['GET'])
def water_levels():
    lat = request.args.get('lat', type=float)
    lng = request.args.get('lng', type=float)
    if lat is None or lng is None:
        return jsonify({'success': False, 'error': 'lat and lng required'}), 400

    radius = request.args.get('radius', 30, type=int)
    levels = USGSWaterAPI.get_water_levels(lat, lng, radius)

    return jsonify({
        'success': True,
        'coordinates': {'lat': lat, 'lng': lng},
        'radius_miles': radius,
        'monitoring_wells': len(levels) if isinstance(levels, list) else 0,
        'data': levels,
        'data_source': 'USGS National Water Information System',
    })


@site_risk_bp.route('/api/v1/risk/water/streamflow', methods=['GET'])
def streamflow():
    lat = request.args.get('lat', type=float)
    lng = request.args.get('lng', type=float)
    if lat is None or lng is None:
        return jsonify({'success': False, 'error': 'lat and lng required'}), 400

    radius = request.args.get('radius', 30, type=int)
    flows = USGSWaterAPI.get_streamflow(lat, lng, radius)

    return jsonify({
        'success': True,
        'coordinates': {'lat': lat, 'lng': lng},
        'radius_miles': radius,
        'stream_gauges': len(flows) if isinstance(flows, list) else 0,
        'data': flows,
        'data_source': 'USGS Instantaneous Values Service',
    })


@site_risk_bp.route('/api/v1/risk/seismic', methods=['GET'])
def seismic_risk():
    lat = request.args.get('lat', type=float)
    lng = request.args.get('lng', type=float)
    if lat is None or lng is None:
        return jsonify({'success': False, 'error': 'lat and lng required'}), 400

    risk = USGSEarthquakeAPI.calculate_seismic_risk(lat, lng)

    return jsonify({
        'success': True,
        'coordinates': {'lat': lat, 'lng': lng},
        **risk,
    })


@site_risk_bp.route('/api/v1/risk/seismic/history', methods=['GET'])
def seismic_history():
    lat = request.args.get('lat', type=float)
    lng = request.args.get('lng', type=float)
    if lat is None or lng is None:
        return jsonify({'success': False, 'error': 'lat and lng required'}), 400

    radius_km = request.args.get('radius_km', 200, type=int)
    years = request.args.get('years', 20, type=int)
    events = USGSEarthquakeAPI.get_seismic_history(lat, lng, radius_km, years)

    return jsonify({
        'success': True,
        'coordinates': {'lat': lat, 'lng': lng},
        'radius_km': radius_km,
        'years': years,
        'total_events': len(events) if isinstance(events, list) else 0,
        'events': events,
        'data_source': 'USGS Earthquake Hazards Program',
    })


@site_risk_bp.route('/api/v1/risk/hazards', methods=['GET'])
def natural_hazards():
    lat = request.args.get('lat', type=float)
    lng = request.args.get('lng', type=float)
    if lat is None or lng is None:
        return jsonify({'success': False, 'error': 'lat and lng required'}), 400

    state = request.args.get('state')
    risk = FEMAHazardAPI.get_hazard_risk(lat, lng, state)

    return jsonify({
        'success': True,
        'coordinates': {'lat': lat, 'lng': lng},
        **risk,
    })


@site_risk_bp.route('/api/v1/risk/climate', methods=['GET'])
def climate_data():
    lat = request.args.get('lat', type=float)
    lng = request.args.get('lng', type=float)
    if lat is None or lng is None:
        return jsonify({'success': False, 'error': 'lat and lng required'}), 400

    state = request.args.get('state')
    climate = NOAAClimateAPI.get_climate_data(lat, lng, state)

    return jsonify({
        'success': True,
        'coordinates': {'lat': lat, 'lng': lng},
        **climate,
    })


@site_risk_bp.route('/api/v1/risk/composite', methods=['GET'])
def composite_risk():
    lat = request.args.get('lat', type=float)
    lng = request.args.get('lng', type=float)
    if lat is None or lng is None:
        return jsonify({'success': False, 'error': 'lat and lng required'}), 400

    state = request.args.get('state')
    result = calculate_composite_risk(lat, lng, state)

    return jsonify({
        'success': True,
        **result,
    })


CITY_COORDINATES = {
    'phoenix': (33.4484, -112.0740), 'dallas': (32.7767, -96.7970),
    'chicago': (41.8781, -87.6298), 'ashburn': (39.0438, -77.4874),
    'los angeles': (34.0522, -118.2437), 'new york': (40.7128, -74.0060),
    'atlanta': (33.7490, -84.3880), 'denver': (39.7392, -104.9903),
    'seattle': (47.6062, -122.3321), 'san jose': (37.3382, -121.8863),
    'silicon valley': (37.3875, -122.0575), 'houston': (29.7604, -95.3698),
    'miami': (25.7617, -80.1918), 'las vegas': (36.1699, -115.1398),
    'portland': (45.5152, -122.6784), 'minneapolis': (44.9778, -93.2650),
    'columbus': (39.9612, -82.9988), 'salt lake city': (40.7608, -111.8910),
    'san antonio': (29.4241, -98.4936), 'reno': (39.5296, -119.8138),
    'des moines': (41.5868, -93.6250), 'omaha': (41.2565, -95.9345),
    'kansas city': (39.0997, -94.5786), 'nashville': (36.1627, -86.7816),
    'richmond': (37.5407, -77.4360), 'boston': (42.3601, -71.0589),
    'sacramento': (38.5816, -121.4944), 'charlotte': (35.2271, -80.8431),
    'austin': (30.2672, -97.7431), 'tampa': (27.9506, -82.4572),
    'raleigh': (35.7796, -78.6382), 'manassas': (38.7509, -77.4753),
    'hillsboro': (45.5229, -122.9898), 'quincy': (47.2343, -119.8526),
    'council bluffs': (41.2619, -95.8608), 'chandler': (33.3062, -111.8413),
    'mesa': (33.4152, -111.8315), 'loudoun county': (39.0835, -77.6525),
    'northern virginia': (39.0438, -77.4874), 'nova': (39.0438, -77.4874),
    'singapore': (1.3521, 103.8198), 'london': (51.5074, -0.1278),
    'frankfurt': (50.1109, 8.6821), 'amsterdam': (52.3676, 4.9041),
    'tokyo': (35.6762, 139.6503), 'sydney': (-33.8688, 151.2093),
    'mumbai': (19.0760, 72.8777), 'toronto': (43.6532, -79.3832),
    'sao paulo': (-23.5505, -46.6333), 'dublin': (53.3498, -6.2603),
}

@site_risk_bp.route('/api/v1/risk/compare', methods=['GET', 'POST'])
def compare_sites():
    _COMPARE_VERSION = "v3"
    sites = []

    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        if 'sites' in data and isinstance(data['sites'], list):
            for s in data['sites']:
                if isinstance(s, dict) and 'lat' in s and 'lng' in s:
                    sites.append({'lat': float(s['lat']), 'lng': float(s['lng']), 'label': s.get('label', f"Site {len(sites)+1}")})
        elif 'locations' in data and isinstance(data['locations'], list):
            for loc in data['locations']:
                name = loc.strip().lower()
                if name in CITY_COORDINATES:
                    lat, lng = CITY_COORDINATES[name]
                    sites.append({'lat': lat, 'lng': lng, 'label': loc.strip()})

    if not sites:
        locations_param = request.args.get('locations', '')
        if locations_param:
            for loc in locations_param.split(','):
                name = loc.strip().lower()
                if name in CITY_COORDINATES:
                    lat, lng = CITY_COORDINATES[name]
                    sites.append({'lat': lat, 'lng': lng, 'label': loc.strip()})
                else:
                    return jsonify({'success': False, 'error': f"Unknown location: '{loc.strip()}'. Use lat,lng coordinates via 'sites' parameter for custom locations.", 'version': _COMPARE_VERSION}), 400

    if not sites:
        sites_param = request.args.get('sites', '')
        if sites_param:
            for pair in sites_param.split(';'):
                parts = pair.strip().split(',')
                if len(parts) >= 2:
                    try:
                        sites.append({'lat': float(parts[0]), 'lng': float(parts[1]), 'label': parts[2] if len(parts) > 2 else f"Site {len(sites)+1}"})
                    except ValueError:
                        continue

    if not sites:
        return jsonify({
            'success': False,
            'error': 'No valid sites provided.',
            'version': _COMPARE_VERSION,
            'debug': {
                'method': request.method,
                'args': dict(request.args),
                'city_dict_size': len(CITY_COORDINATES),
            },
            'usage': {
                'GET_locations': '/api/v1/risk/compare?locations=Phoenix,Dallas,Chicago',
                'GET_coordinates': '/api/v1/risk/compare?sites=33.45,-112.07,Phoenix;32.78,-96.80,Dallas',
                'POST_json': {'locations': ['Phoenix', 'Dallas', 'Chicago']},
                'POST_coordinates': {'sites': [{'lat': 33.45, 'lng': -112.07, 'label': 'Phoenix'}]},
            }
        }), 400

    if len(sites) < 2:
        return jsonify({'success': False, 'error': 'At least 2 sites required for comparison'}), 400
    if len(sites) > 5:
        return jsonify({'success': False, 'error': 'Maximum 5 sites for comparison'}), 400

    def _score_site(site):
        risk = calculate_composite_risk(site['lat'], site['lng'])
        return {
            'label': site['label'],
            'lat': site['lat'],
            'lng': site['lng'],
            'composite_score': risk.get('composite_site_risk_score', 0),
            'grade': risk.get('site_grade', 'N/A'),
            'partial': risk.get('partial', False),
            'data_sources': risk.get('data_sources', {}),
            'water_score': risk.get('water', {}).get('water_availability_score', 0),
            'seismic_score': risk.get('seismic', {}).get('seismic_safety_score', 0),
            'hazard_score': risk.get('natural_hazards', {}).get('natural_hazard_safety_score', 0),
            'climate_score': risk.get('climate', {}).get('cooling_efficiency_score', 0),
            'weakest_factor': risk.get('weakest_factor', ''),
            'strongest_factor': risk.get('strongest_factor', ''),
            'recommendation': risk.get('recommendation', ''),
        }

    results = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(_score_site, site): site for site in sites}
        for future in as_completed(futures):
            try:
                results.append(future.result(timeout=30))
            except Exception as e:
                site = futures[future]
                logger.warning(f"Site comparison failed for {site['label']}: {e}")
                results.append({
                    'label': site['label'],
                    'lat': site['lat'],
                    'lng': site['lng'],
                    'composite_score': 0,
                    'grade': 'N/A',
                    'partial': True,
                    'error': f'Scoring failed: {str(e)}',
                })

    results.sort(key=lambda x: x.get('composite_score', 0), reverse=True)
    any_partial = any(r.get('partial', False) for r in results)

    return jsonify({
        'success': True,
        'sites_compared': len(results),
        'best_site': results[0]['label'] if results else 'N/A',
        'partial': any_partial,
        'rankings': results,
        'generated_at': datetime.utcnow().isoformat(),
    })


@site_risk_bp.route('/api/v1/risk/layers', methods=['GET'])
def risk_layers():
    return jsonify({
        'success': True,
        'risk_assessment_apis': {
            'water_risk': {
                'endpoint': '/api/v1/risk/water',
                'params': 'lat, lng',
                'description': 'Water availability and stress assessment using USGS groundwater, water levels, and streamflow data',
                'data_source': 'USGS National Water Information System',
            },
            'water_levels': {
                'endpoint': '/api/v1/risk/water/levels',
                'params': 'lat, lng, radius (default 30)',
                'description': 'Nearby groundwater monitoring well levels with depth measurements',
                'data_source': 'USGS Instantaneous Values Service',
            },
            'streamflow': {
                'endpoint': '/api/v1/risk/water/streamflow',
                'params': 'lat, lng, radius (default 30)',
                'description': 'Nearby stream/river flow rates with 7-day statistics',
                'data_source': 'USGS Instantaneous Values Service',
            },
            'seismic_risk': {
                'endpoint': '/api/v1/risk/seismic',
                'params': 'lat, lng',
                'description': 'Seismic risk assessment based on 20-year earthquake history within 200km',
                'data_source': 'USGS Earthquake Hazards Program',
            },
            'seismic_history': {
                'endpoint': '/api/v1/risk/seismic/history',
                'params': 'lat, lng, radius_km (default 200), years (default 20)',
                'description': 'Historical earthquake events near location',
                'data_source': 'USGS Earthquake Hazards Program',
            },
            'natural_hazards': {
                'endpoint': '/api/v1/risk/hazards',
                'params': 'lat, lng, state (optional)',
                'description': 'Multi-hazard risk assessment: flood, tornado, hurricane, earthquake, wildfire, winter storm, hail',
                'data_source': 'FEMA National Risk Index (modeled)',
            },
            'climate': {
                'endpoint': '/api/v1/risk/climate',
                'params': 'lat, lng, state (optional)',
                'description': 'Climate data with DC-specific metrics: CDD, free cooling hours, PUE estimate',
                'data_source': 'NOAA Climate Normals (30-year averages)',
            },
            'composite_risk': {
                'endpoint': '/api/v1/risk/composite',
                'params': 'lat, lng, state (optional)',
                'description': 'Combined site risk score (0-100) with A-F grade factoring water, seismic, hazard, and climate',
                'weights': 'Water 30%, Seismic 25%, Hazards 25%, Climate 20%',
            },
            'compare_sites': {
                'endpoint': '/api/v1/risk/compare',
                'params': 'sites (format: lat1,lng1,label1;lat2,lng2,label2)',
                'description': 'Compare 2-5 sites side-by-side with rankings',
            },
        },
        'total_endpoints': 10,
        'version': 'v1',
    })


def register_site_risk_routes(app):
    app.register_blueprint(site_risk_bp)
    logger.info("✅ Site Risk Assessment API registered (10 endpoints: water, seismic, hazards, climate, composite)")
