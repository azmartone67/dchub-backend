"""
Global Power APIs Integration
- Electricity Maps (190+ countries carbon intensity)  [token: ELECTRICITY_MAPS_API_TOKEN]
- UK Carbon Intensity API (UK regional data)          [free, no token]
- ENTSO-E (European grid data)                        [token: ENTSOE_API_TOKEN]
- ENTSOG (European gas transmission data)             [free public API]
- AEMO NEM (Australian electricity market)            [free public API]

FAIL-CLOSED CONTRACT
--------------------
This module NEVER returns a fabricated number labeled as a real source.
A source that requires an API token we do not have returns an honest
``{"source_unavailable": True, "source": "...", "needs_token_env": "...",
"estimated": False}`` so callers can render "data unavailable" instead of a
fake reading. Any value that is genuinely an estimate is labeled
``"estimated": True`` with ``"source": "estimate"`` — never the real
source name.

Verifiable upstream endpoints used here:
  Electricity Maps : https://api.electricitymap.org/v3  (header: auth-token)
  UK Carbon Int.   : https://api.carbonintensity.org.uk (no auth)
  ENTSO-E          : https://web-api.tp.entsoe.eu/api    (?securityToken=)
  ENTSOG           : https://transparency.entsog.eu/api/v1 (no auth)
  AEMO NEM         : https://visualisations.aemo.com.au/aemo/apps/api/report/ELEC_NEM_SUMMARY (no auth)
"""

import os
import requests
import logging
from datetime import datetime, timedelta
from functools import lru_cache
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

global_power_bp = Blueprint('global_power', __name__)

CACHE_DURATION = 300

_cache = {}

def get_cached(key, fetch_func, ttl=CACHE_DURATION):
    """Simple cache with TTL"""
    now = datetime.now()
    if key in _cache:
        data, expiry = _cache[key]
        if now < expiry:
            return data
    try:
        data = fetch_func()
        _cache[key] = (data, now + timedelta(seconds=ttl))
        return data
    except Exception as e:
        logger.error(f"Cache fetch error for {key}: {e}")
        if key in _cache:
            return _cache[key][0]
        return None


# ─────────────────────────────────────────────────────────────────────
# r67 (2026-06-02): ENTSO-E token — accept BOTH env-var names.
#
# The token is read in three places (this module's ENTSOEAPI, the separate
# parser in routes/international_ingestion.py, and the alive/status probes).
# Railway currently sets ENTSOE_API_TOKEN, but a future rename to the shorter
# ENTSOE_TOKEN (the name the entsoe-py library uses) would silently break grid
# data with no error — the fail-closed path just starts returning
# source_unavailable. Read BOTH names from ONE helper so either works and a
# rename can't silently regress. Canonical name stays ENTSOE_API_TOKEN (what we
# report in needs_token_env). Whitespace-stripped; empty string treated as unset.
# ─────────────────────────────────────────────────────────────────────
ENTSOE_TOKEN_ENVS = ("ENTSOE_API_TOKEN", "ENTSOE_TOKEN")


def entsoe_token():
    """Return the ENTSO-E security token from either accepted env var, or None.
    Single source of truth shared across every ENTSO-E read path."""
    for name in ENTSOE_TOKEN_ENVS:
        val = (os.environ.get(name) or "").strip()
        if val:
            return val
    return None


class ElectricityMapsAPI:
    """Electricity Maps - Global carbon intensity and power mix.

    Requires an API token (free + paid tiers) sent as the ``auth-token``
    header per https://docs.electricitymaps.com/. Without the token the
    public v3 API returns 401, so we FAIL CLOSED rather than invent a
    carbon-intensity number.
    """

    BASE_URL = "https://api.electricitymap.org/v3"
    TOKEN_ENV = "ELECTRICITY_MAPS_API_TOKEN"
    SOURCE = "Electricity Maps (api.electricitymap.org)"

    @classmethod
    def _token(cls):
        return os.environ.get(cls.TOKEN_ENV)

    @classmethod
    def _auth_headers(cls):
        token = cls._token()
        return {'auth-token': token} if token else {}

    @classmethod
    def _unavailable(cls, zone, detail):
        """Honest fail-closed payload — no fabricated values."""
        return {
            'source_unavailable': True,
            'source': cls.SOURCE,
            'needs_token_env': cls.TOKEN_ENV,
            'zone': zone,
            'estimated': False,
            'detail': detail,
        }

    ZONES = {
        'US-CAL-CISO': 'California (CAISO)',
        'US-TEX-ERCO': 'Texas (ERCOT)',
        'US-NY-NYIS': 'New York (NYISO)',
        'US-MIDA-PJM': 'PJM Interconnection',
        'US-MIDW-MISO': 'MISO',
        'US-NE-ISNE': 'ISO New England',
        'US-SW-AZPS': 'Arizona (APS)',
        'US-NW-BPAT': 'Pacific Northwest (BPA)',
        'DE': 'Germany',
        'FR': 'France',
        'GB': 'United Kingdom',
        'NL': 'Netherlands',
        'IE': 'Ireland',
        'ES': 'Spain',
        'IT-NO': 'Italy (North)',
        'SE': 'Sweden',
        'NO': 'Norway',
        'DK-DK1': 'Denmark (West)',
        'FI': 'Finland',
        'PL': 'Poland',
        'AT': 'Austria',
        'CH': 'Switzerland',
        'BE': 'Belgium',
        'PT': 'Portugal',
        'JP-TK': 'Japan (Tokyo)',
        'AU-NSW': 'Australia (NSW)',
        'AU-VIC': 'Australia (Victoria)',
        'SG': 'Singapore',
        'IN-DL': 'India (Delhi)',
        'BR-S': 'Brazil (South)',
    }
    
    @classmethod
    def get_carbon_intensity(cls, zone='US-CAL-CISO'):
        """Get current carbon intensity for a zone.

        Token-gated. Returns the live Electricity Maps payload, or an honest
        ``source_unavailable`` dict when the token is missing / the call fails.
        """
        def fetch():
            if not cls._token():
                return cls._unavailable(zone, f"{cls.TOKEN_ENV} not set")
            url = f"{cls.BASE_URL}/carbon-intensity/latest"
            params = {'zone': zone}
            try:
                resp = requests.get(url, params=params, headers=cls._auth_headers(), timeout=10)
            except requests.RequestException as e:
                return cls._unavailable(zone, f"request error: {type(e).__name__}")
            if resp.status_code == 200:
                return resp.json()
            return cls._unavailable(zone, f"upstream HTTP {resp.status_code}")
        return get_cached(f"em_carbon_{zone}", fetch)

    @classmethod
    def get_power_breakdown(cls, zone='US-CAL-CISO'):
        """Get power generation breakdown by source.

        Token-gated; fails closed with ``source_unavailable`` (never a
        fabricated generation mix) when the token is missing or upstream errors.
        """
        def fetch():
            if not cls._token():
                return cls._unavailable(zone, f"{cls.TOKEN_ENV} not set")
            url = f"{cls.BASE_URL}/power-breakdown/latest"
            params = {'zone': zone}
            try:
                resp = requests.get(url, params=params, headers=cls._auth_headers(), timeout=10)
            except requests.RequestException as e:
                return cls._unavailable(zone, f"request error: {type(e).__name__}")
            if resp.status_code == 200:
                return resp.json()
            return cls._unavailable(zone, f"upstream HTTP {resp.status_code}")
        return get_cached(f"em_power_{zone}", fetch)

    @classmethod
    def get_zones(cls):
        """Get available zones.

        The zone *list* is static reference metadata (zone codes + human
        labels), not a measured value, so returning ``cls.ZONES`` when the
        live ``/zones`` directory is unreachable is not a fabricated reading.
        """
        def fetch():
            url = f"{cls.BASE_URL}/zones"
            try:
                resp = requests.get(url, headers=cls._auth_headers(), timeout=10)
                if resp.status_code == 200:
                    return resp.json()
            except requests.RequestException:
                pass
            return cls.ZONES
        return get_cached("em_zones", fetch, ttl=3600)


class UKCarbonIntensityAPI:
    """UK National Grid Carbon Intensity API - 100% Free"""
    
    BASE_URL = "https://api.carbonintensity.org.uk"
    
    REGIONS = {
        1: 'North Scotland',
        2: 'South Scotland', 
        3: 'North West England',
        4: 'North East England',
        5: 'Yorkshire',
        6: 'North Wales & Merseyside',
        7: 'South Wales',
        8: 'West Midlands',
        9: 'East Midlands',
        10: 'East England',
        11: 'South West England',
        12: 'South England',
        13: 'London',
        14: 'South East England',
    }
    
    @classmethod
    def get_current_intensity(cls):
        """Get current national carbon intensity"""
        def fetch():
            url = f"{cls.BASE_URL}/intensity"
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if data.get('data') and len(data['data']) > 0:
                    return data['data'][0]
            return None
        return get_cached("uk_intensity_current", fetch)
    
    @classmethod
    def get_regional_intensity(cls, region_id=None):
        """Get regional carbon intensity"""
        def fetch():
            url = f"{cls.BASE_URL}/regional"
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                return resp.json()
            return None
        
        data = get_cached("uk_regional", fetch)
        if data and region_id:
            regions = data.get('data', [{}])[0].get('regions', [])
            for r in regions:
                if r.get('regionid') == region_id:
                    return r
        return data
    
    @classmethod
    def get_generation_mix(cls):
        """Get current UK generation mix"""
        def fetch():
            url = f"{cls.BASE_URL}/generation"
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                return resp.json()
            return None
        return get_cached("uk_generation", fetch)
    
    @classmethod
    def get_forecast(cls, hours=24):
        """Get carbon intensity forecast"""
        def fetch():
            url = f"{cls.BASE_URL}/intensity/date/{datetime.now().strftime('%Y-%m-%d')}"
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                return resp.json()
            return None
        return get_cached(f"uk_forecast_{hours}", fetch, ttl=1800)
    
    @classmethod
    def get_stats(cls, from_date=None, to_date=None):
        """Get statistics for date range"""
        if not from_date:
            from_date = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        if not to_date:
            to_date = datetime.now().strftime('%Y-%m-%d')
        
        def fetch():
            url = f"{cls.BASE_URL}/intensity/stats/{from_date}/{to_date}"
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                return resp.json()
            return None
        return get_cached(f"uk_stats_{from_date}_{to_date}", fetch, ttl=3600)


class ENTSOEAPI:
    """ENTSO-E Transparency Platform - European Grid Data.

    Token-gated (``securityToken`` query param, free registration at
    https://transparency.entsoe.eu/usrm/user/createPublicUser). Every
    measured value (load, generation, price, cross-border flow) is fetched
    live from the Transparency Platform XML API. When the token is missing
    or upstream fails we FAIL CLOSED with an honest ``source_unavailable``
    payload — no fabricated MW or EUR/MWh numbers.
    """

    BASE_URL = "https://web-api.tp.entsoe.eu/api"
    TOKEN_ENV = "ENTSOE_API_TOKEN"          # canonical name (reported in needs_token_env)
    TOKEN_ENVS = ENTSOE_TOKEN_ENVS          # both accepted names
    SOURCE = "ENTSO-E Transparency Platform"
    TIMEOUT_S = 12

    @classmethod
    def _token(cls):
        # r67: accept ENTSOE_API_TOKEN OR ENTSOE_TOKEN via the shared helper so
        # a future rename can't silently fail the European grid feed.
        return entsoe_token()

    @classmethod
    def _unavailable(cls, area, detail, **extra):
        """Honest fail-closed payload for an ENTSO-E area — no fake values."""
        payload = {
            'source_unavailable': True,
            'source': cls.SOURCE,
            'needs_token_env': cls.TOKEN_ENV,
            'area': area,
            'area_name': cls.AREAS.get(area, {}).get('name', area),
            'estimated': False,
            'detail': detail,
            'timestamp': datetime.now().isoformat(),
        }
        payload.update(extra)
        return payload

    @staticmethod
    def _strip_ns(tag):
        return tag.split('}', 1)[1] if '}' in tag else tag

    @classmethod
    def _window(cls):
        """ENTSO-E wants YYYYMMDDHHmm UTC. Pull last 24h so a current
        reading is available even off-peak/weekends."""
        now = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
        start = now - timedelta(hours=24)
        return start.strftime('%Y%m%d%H%M'), now.strftime('%Y%m%d%H%M')

    @classmethod
    def _fetch_xml(cls, params):
        """Single live call to the Transparency Platform. Returns raw XML
        body, or None on missing-token / non-200 / network error."""
        token = cls._token()
        if not token:
            return None
        qs = '&'.join(f"{k}={v}" for k, v in params.items())
        url = f"{cls.BASE_URL}?securityToken={token}&{qs}"
        try:
            resp = requests.get(url, timeout=cls.TIMEOUT_S,
                                headers={'User-Agent': 'dchub-entsoe/1.0'})
            if resp.status_code == 200:
                return resp.text
            logger.warning("ENTSO-E %s %s -> HTTP %s",
                           params.get('documentType', '?'),
                           params.get('in_Domain', params.get('outBiddingZone_Domain', '?')),
                           resp.status_code)
        except requests.RequestException as e:
            logger.warning("ENTSO-E request error: %s", type(e).__name__)
        return None

    @classmethod
    def _collect_series(cls, xml_body):
        """Parse TimeSeries → Period → Point and return a list of
        ``(psr_type_or_None, quantities[])`` tuples. Namespace-agnostic."""
        import xml.etree.ElementTree as ET
        series = []
        try:
            root = ET.fromstring(xml_body)
        except ET.ParseError:
            return series
        for ts in root.iter():
            if cls._strip_ns(ts.tag) != 'TimeSeries':
                continue
            psr_type = None
            qtys = []
            for el in ts.iter():
                name = cls._strip_ns(el.tag)
                if name == 'psrType' and el.text:
                    psr_type = el.text.strip()
                elif name == 'Point':
                    for child in el:
                        if cls._strip_ns(child.tag) == 'quantity' and child.text:
                            try:
                                qtys.append(float(child.text))
                            except ValueError:
                                pass
            series.append((psr_type, qtys))
        return series

    @classmethod
    def _collect_points(cls, xml_body):
        """Flat list of every <quantity> across all Points (load/price docs)."""
        pts = []
        for _psr, qtys in cls._collect_series(xml_body):
            pts.extend(qtys)
        return pts

    AREAS = {
        'DE_LU': {'name': 'Germany/Luxembourg', 'eic': '10Y1001A1001A82H'},
        'FR': {'name': 'France', 'eic': '10YFR-RTE------C'},
        'ES': {'name': 'Spain', 'eic': '10YES-REE------0'},
        'IT_NORD': {'name': 'Italy North', 'eic': '10Y1001A1001A73I'},
        # r67 (2026-06-02): full set of Italian bidding zones so Italy returns
        # live ENTSO-E data instead of failing closed for everything south of
        # the North zone. EIC codes verified against the canonical entsoe-py
        # mappings (EnergieID/entsoe-py) — IT_NORD already matched, confirming
        # the source. If ENTSO-E genuinely has no data for a zone, get_load /
        # get_generation / get_prices still fail closed honestly
        # (source_unavailable) — these mappings never fabricate a reading.
        'IT_CNOR': {'name': 'Italy Centre-North', 'eic': '10Y1001A1001A70O'},
        'IT_CSUD': {'name': 'Italy Centre-South', 'eic': '10Y1001A1001A71M'},
        'IT_SUD':  {'name': 'Italy South', 'eic': '10Y1001A1001A788'},
        'IT_SICI': {'name': 'Italy Sicily', 'eic': '10Y1001A1001A75E'},
        'IT_SARD': {'name': 'Italy Sardinia', 'eic': '10Y1001A1001A74G'},
        'IT_CALA': {'name': 'Italy Calabria', 'eic': '10Y1001C--00096J'},
        'NL': {'name': 'Netherlands', 'eic': '10YNL----------L'},
        'BE': {'name': 'Belgium', 'eic': '10YBE----------2'},
        'AT': {'name': 'Austria', 'eic': '10YAT-APG------L'},
        'CH': {'name': 'Switzerland', 'eic': '10YCH-SWISSGRIDZ'},
        'PL': {'name': 'Poland', 'eic': '10YPL-AREA-----S'},
        'CZ': {'name': 'Czech Republic', 'eic': '10YCZ-CEPS-----N'},
        'DK1': {'name': 'Denmark West', 'eic': '10YDK-1--------W'},
        'DK2': {'name': 'Denmark East', 'eic': '10YDK-2--------M'},
        'SE1': {'name': 'Sweden North', 'eic': '10Y1001A1001A44P'},
        'SE2': {'name': 'Sweden Central North', 'eic': '10Y1001A1001A45N'},
        'SE3': {'name': 'Sweden Central South', 'eic': '10Y1001A1001A46L'},
        'SE4': {'name': 'Sweden South', 'eic': '10Y1001A1001A47J'},
        'NO1': {'name': 'Norway East', 'eic': '10YNO-1--------2'},
        'NO2': {'name': 'Norway South', 'eic': '10YNO-2--------T'},
        'FI': {'name': 'Finland', 'eic': '10YFI-1--------U'},
        'GB': {'name': 'Great Britain', 'eic': '10YGB----------A'},
        'IE': {'name': 'Ireland', 'eic': '10YIE-1001A00010'},
        'PT': {'name': 'Portugal', 'eic': '10YPT-REN------W'},
        'GR': {'name': 'Greece', 'eic': '10YGR-HTSO-----Y'},
    }
    
    GENERATION_TYPES = {
        'B01': 'Biomass',
        'B02': 'Lignite',
        'B03': 'Fossil Coal-derived gas',
        'B04': 'Fossil Gas',
        'B05': 'Fossil Hard coal',
        'B06': 'Fossil Oil',
        'B09': 'Geothermal',
        'B10': 'Hydro Pumped Storage',
        'B11': 'Hydro Run-of-river',
        'B12': 'Hydro Water Reservoir',
        'B13': 'Marine',
        'B14': 'Nuclear',
        'B15': 'Other renewable',
        'B16': 'Solar',
        'B17': 'Waste',
        'B18': 'Wind Offshore',
        'B19': 'Wind Onshore',
        'B20': 'Other',
    }
    
    @classmethod
    def get_areas(cls):
        """Get available bidding zones"""
        return {
            'areas': cls.AREAS,
            'count': len(cls.AREAS),
            'coverage': 'Pan-European (EU + UK + Norway + Switzerland)'
        }
    
    @classmethod
    def get_load(cls, area='DE_LU'):
        """Get actual total load for an area (live; fails closed).

        Live ENTSO-E "Actual Total Load" (documentType A65, processType A16)
        over the last 24h. current_mw = latest point; peak/min from the
        24h window. No token or no data -> ``source_unavailable`` (no fakes).
        """
        def fetch():
            area_info = cls.AREAS.get(area)
            if not area_info:
                return cls._unavailable(area, "unknown area code")
            if not cls._token():
                return cls._unavailable(area, f"{cls.TOKEN_ENV} not set")
            start, end = cls._window()
            xml_body = cls._fetch_xml({
                'documentType':          'A65',
                'processType':           'A16',
                'outBiddingZone_Domain': area_info['eic'],
                'periodStart':           start,
                'periodEnd':             end,
            })
            if not xml_body:
                return cls._unavailable(area, "upstream returned no data")
            points = cls._collect_points(xml_body)
            if not points:
                return cls._unavailable(area, "no load points in response")
            return {
                'area': area,
                'area_name': area_info.get('name', area),
                'timestamp': datetime.now().isoformat(),
                'load_mw': round(points[-1], 0),
                'peak_mw': round(max(points), 0),
                'min_mw': round(min(points), 0),
                'unit': 'MW',
                'window_hours': 24,
                'estimated': False,
                'source': cls.SOURCE,
            }
        return get_cached(f"entsoe_load_{area}", fetch, ttl=900)
    
    RENEWABLE_TYPES = frozenset({
        'Biomass', 'Geothermal', 'Hydro Pumped Storage', 'Hydro Run-of-river',
        'Hydro Water Reservoir', 'Marine', 'Other renewable', 'Solar',
        'Wind Offshore', 'Wind Onshore',
    })

    @classmethod
    def get_generation(cls, area='DE_LU'):
        """Get generation by type for an area (live; fails closed).

        Live ENTSO-E "Aggregated Generation per Type" (documentType A75,
        processType A16). PSR B-codes are mapped to fuel names via
        ``GENERATION_TYPES``; the latest point per type is used. No token or
        no data -> ``source_unavailable`` (never a fabricated mix).
        """
        def fetch():
            area_info = cls.AREAS.get(area)
            if not area_info:
                return cls._unavailable(area, "unknown area code")
            if not cls._token():
                return cls._unavailable(area, f"{cls.TOKEN_ENV} not set")
            start, end = cls._window()
            xml_body = cls._fetch_xml({
                'documentType': 'A75',
                'processType':  'A16',
                'in_Domain':    area_info['eic'],
                'periodStart':  start,
                'periodEnd':    end,
            })
            if not xml_body:
                return cls._unavailable(area, "upstream returned no data")
            by_type = {}
            for psr, qtys in cls._collect_series(xml_body):
                if not psr or not qtys:
                    continue
                fuel = cls.GENERATION_TYPES.get(psr, f"PSR {psr}")
                # Latest point for this type is the current output.
                by_type[fuel] = round(by_type.get(fuel, 0.0) + qtys[-1], 0)
            if not by_type:
                return cls._unavailable(area, "no generation points in response")
            total = sum(by_type.values())
            renewable_pct = None
            if total > 0:
                renewable_pct = round(
                    sum(v for k, v in by_type.items() if k in cls.RENEWABLE_TYPES) / total * 100, 1
                )
            return {
                'area': area,
                'area_name': area_info.get('name', area),
                'timestamp': datetime.now().isoformat(),
                'generation_by_type': by_type,
                'total_generation_mw': round(total, 0),
                'renewable_percentage': renewable_pct,
                'unit': 'MW',
                'estimated': False,
                'source': cls.SOURCE,
            }
        return get_cached(f"entsoe_gen_{area}", fetch, ttl=900)
    
    @classmethod
    def get_prices(cls, area='DE_LU'):
        """Get day-ahead electricity prices for an area (live; fails closed).

        Live ENTSO-E "Day-ahead Prices" (documentType A44; in_Domain and
        out_Domain both set to the bidding-zone EIC). current_price = latest
        hourly point; min/max over the published curve. No token or no data
        -> ``source_unavailable`` (never a synthetic price).
        """
        def fetch():
            area_info = cls.AREAS.get(area)
            if not area_info:
                return cls._unavailable(area, "unknown area code")
            if not cls._token():
                return cls._unavailable(area, f"{cls.TOKEN_ENV} not set")
            start, end = cls._window()
            xml_body = cls._fetch_xml({
                'documentType': 'A44',
                'in_Domain':    area_info['eic'],
                'out_Domain':   area_info['eic'],
                'periodStart':  start,
                'periodEnd':    end,
            })
            if not xml_body:
                return cls._unavailable(area, "upstream returned no data")
            prices = cls._collect_points(xml_body)
            if not prices:
                return cls._unavailable(area, "no price points in response")
            return {
                'area': area,
                'area_name': area_info.get('name', area),
                'timestamp': datetime.now().isoformat(),
                'price_eur_mwh': round(prices[-1], 2),
                'min_eur_mwh': round(min(prices), 2),
                'max_eur_mwh': round(max(prices), 2),
                'currency': 'EUR',
                'unit': 'EUR/MWh',
                'market': 'Day-Ahead',
                'estimated': False,
                'source': cls.SOURCE,
            }
        return get_cached(f"entsoe_price_{area}", fetch, ttl=900)
    
    NEIGHBORS = {
        'DE_LU': ['FR', 'NL', 'BE', 'AT', 'CH', 'PL', 'CZ', 'DK1', 'DK2'],
        'FR': ['DE_LU', 'ES', 'IT_NORD', 'BE', 'CH', 'GB'],
        'GB': ['FR', 'NL', 'BE', 'IE'],
    }

    @classmethod
    def get_cross_border_flows(cls, area='DE_LU'):
        """Get cross-border physical flows for an area (live; fails closed).

        Live ENTSO-E "Physical Flows" (documentType A11). For each neighbor
        we query both directions (export = out_Domain→in_Domain, import =
        reverse) and take the net of the latest points. No token -> honest
        ``source_unavailable``; neighbors with no published data are listed
        under ``unavailable_neighbors`` rather than given a fabricated value.
        """
        def fetch():
            area_info = cls.AREAS.get(area)
            if not area_info:
                return cls._unavailable(area, "unknown area code")
            if not cls._token():
                return cls._unavailable(area, f"{cls.TOKEN_ENV} not set",
                                        note='Positive = export from area, Negative = import to area')
            start, end = cls._window()
            home_eic = area_info['eic']
            flows = {}
            unavailable = []
            for n in cls.NEIGHBORS.get(area, []):
                n_info = cls.AREAS.get(n)
                if not n_info:
                    unavailable.append(n)
                    continue
                n_eic = n_info['eic']
                export_xml = cls._fetch_xml({
                    'documentType': 'A11',
                    'out_Domain':   home_eic,
                    'in_Domain':    n_eic,
                    'periodStart':  start,
                    'periodEnd':    end,
                })
                import_xml = cls._fetch_xml({
                    'documentType': 'A11',
                    'out_Domain':   n_eic,
                    'in_Domain':    home_eic,
                    'periodStart':  start,
                    'periodEnd':    end,
                })
                exp_pts = cls._collect_points(export_xml) if export_xml else []
                imp_pts = cls._collect_points(import_xml) if import_xml else []
                if not exp_pts and not imp_pts:
                    unavailable.append(n)
                    continue
                net = (exp_pts[-1] if exp_pts else 0.0) - (imp_pts[-1] if imp_pts else 0.0)
                flows[n] = round(net, 0)
            if not flows:
                return cls._unavailable(area, "no flow data for any neighbor",
                                        note='Positive = export from area, Negative = import to area')
            return {
                'area': area,
                'area_name': area_info.get('name', area),
                'timestamp': datetime.now().isoformat(),
                'flows': flows,
                'unavailable_neighbors': unavailable,
                'net_flow_mw': round(sum(flows.values()), 0),
                'note': 'Positive = export from area, Negative = import to area',
                'unit': 'MW',
                'estimated': False,
                'source': cls.SOURCE,
            }
        return get_cached(f"entsoe_flows_{area}", fetch, ttl=900)


class ENTSOGAPI:
    """ENTSOG Transparency Platform - European gas transmission data.

    Free public JSON API (no token required) at
    https://transparency.entsog.eu/api/v1/. We fetch live operational data
    (physical flows / nominations at transmission points). On any network or
    parse failure we FAIL CLOSED with an honest ``source_unavailable`` payload
    — never a fabricated gas-flow number.

    Verified endpoint shape (2026-06-02):
      GET /api/v1/operationaldata?... -> {"meta": {...},
          "operationaldata": [{"pointKey","pointLabel","operatorLabel",
          "indicator","value","unit","periodFrom","periodTo", ...}, ...]}
    Indicators are documented at
      https://transparency.entsog.eu/  (e.g. "Physical Flow", "Nomination").
    """

    BASE_URL = "https://transparency.entsog.eu/api/v1"
    # Optional override token — the public API needs none, but honoring an
    # env var lets ops point at a credentialed proxy/mirror without a code change.
    TOKEN_ENV = "ENTSOG_API_TOKEN"
    SOURCE = "ENTSOG Transparency Platform (transparency.entsog.eu)"
    TIMEOUT_S = 15

    @classmethod
    def _headers(cls):
        token = os.environ.get(cls.TOKEN_ENV)
        h = {'Accept': 'application/json', 'User-Agent': 'dchub-entsog/1.0'}
        if token:
            h['Authorization'] = f"Bearer {token}"
        return h

    @classmethod
    def _unavailable(cls, detail, **extra):
        payload = {
            'source_unavailable': True,
            'source': cls.SOURCE,
            'needs_token_env': None,  # public API; no token required
            'estimated': False,
            'detail': detail,
            'timestamp': datetime.now().isoformat(),
        }
        payload.update(extra)
        return payload

    @classmethod
    def get_operational_data(cls, indicator='Physical Flow', limit=100):
        """Live operational gas-flow data from ENTSOG (fails closed).

        Returns the parsed ``operationaldata`` records straight from the
        public API, or an honest ``source_unavailable`` payload on any
        failure. No values are synthesized.
        """
        def fetch():
            url = f"{cls.BASE_URL}/operationaldata"
            params = {
                'limit': max(1, min(int(limit), 1000)),
                'indicator': indicator,
                'periodType': 'day',
            }
            try:
                resp = requests.get(url, params=params, headers=cls._headers(),
                                    timeout=cls.TIMEOUT_S)
            except requests.RequestException as e:
                return cls._unavailable(f"request error: {type(e).__name__}",
                                        indicator=indicator)
            if resp.status_code != 200:
                return cls._unavailable(f"upstream HTTP {resp.status_code}",
                                        indicator=indicator)
            try:
                body = resp.json()
            except ValueError:
                return cls._unavailable("non-JSON response", indicator=indicator)
            records = body.get('operationaldata')
            if records is None:
                return cls._unavailable("no operationaldata key in response",
                                        indicator=indicator)
            return {
                'indicator': indicator,
                'timestamp': datetime.now().isoformat(),
                'count': len(records),
                'records': records,
                'unit': 'kWh/d',  # ENTSOG default; authoritative unit is per-record 'unit'
                'estimated': False,
                'source': cls.SOURCE,
            }
        return get_cached(f"entsog_op_{indicator}_{limit}", fetch, ttl=1800)


class AEMONemAPI:
    """AEMO NEM - Australian National Electricity Market (live, free).

    AEMO publishes a free, unauthenticated JSON summary of the five NEM
    regions (NSW1, QLD1, SA1, TAS1, VIC1) at
    https://visualisations.aemo.com.au/aemo/apps/api/report/ELEC_NEM_SUMMARY.
    We fetch it live; on any failure we FAIL CLOSED with an honest
    ``source_unavailable`` payload — never a fabricated price or demand.

    Verified response shape (2026-06-02):
      {"ELEC_NEM_SUMMARY": [{"REGIONID","PRICE","TOTALDEMAND",
        "SCHEDULEDGENERATION","SEMISCHEDULEDGENERATION","NETINTERCHANGE",
        "SETTLEMENTDATE", ...}, ...],
       "ELEC_NEM_SUMMARY_PRICES": [{"REGIONID","RRP", ...}, ...], ...}
    """

    BASE_URL = "https://visualisations.aemo.com.au/aemo/apps/api/report"
    SUMMARY_PATH = "ELEC_NEM_SUMMARY"
    # Public API requires no token; honor an override for a credentialed mirror.
    TOKEN_ENV = "AEMO_API_TOKEN"
    SOURCE = "AEMO NEM (visualisations.aemo.com.au)"
    TIMEOUT_S = 15

    REGIONS = {
        'NSW1': 'New South Wales',
        'QLD1': 'Queensland',
        'SA1':  'South Australia',
        'TAS1': 'Tasmania',
        'VIC1': 'Victoria',
    }

    @classmethod
    def _headers(cls):
        token = os.environ.get(cls.TOKEN_ENV)
        h = {'Accept': 'application/json', 'User-Agent': 'dchub-aemo/1.0'}
        if token:
            h['Authorization'] = f"Bearer {token}"
        return h

    @classmethod
    def _unavailable(cls, detail, **extra):
        payload = {
            'source_unavailable': True,
            'source': cls.SOURCE,
            'needs_token_env': None,  # public API; no token required
            'estimated': False,
            'detail': detail,
            'timestamp': datetime.now().isoformat(),
        }
        payload.update(extra)
        return payload

    @classmethod
    def get_nem_summary(cls, region=None):
        """Live NEM regional summary (price, demand, generation) — fails closed.

        Returns AEMO's published per-region records verbatim (optionally
        filtered to one REGIONID), or an honest ``source_unavailable`` payload
        on any failure. No values are synthesized.
        """
        def fetch():
            url = f"{cls.BASE_URL}/{cls.SUMMARY_PATH}"
            try:
                resp = requests.get(url, headers=cls._headers(), timeout=cls.TIMEOUT_S)
            except requests.RequestException as e:
                return cls._unavailable(f"request error: {type(e).__name__}")
            if resp.status_code != 200:
                return cls._unavailable(f"upstream HTTP {resp.status_code}")
            try:
                body = resp.json()
            except ValueError:
                return cls._unavailable("non-JSON response")
            rows = body.get(cls.SUMMARY_PATH)
            if rows is None:
                return cls._unavailable(f"no {cls.SUMMARY_PATH} key in response")
            return {
                'timestamp': datetime.now().isoformat(),
                'regions': rows,
                'prices': body.get('ELEC_NEM_SUMMARY_PRICES', []),
                'count': len(rows),
                'unit': 'AUD/MWh (PRICE/RRP); MW (TOTALDEMAND/generation)',
                'estimated': False,
                'source': cls.SOURCE,
            }

        data = get_cached("aemo_nem_summary", fetch, ttl=300)
        if region and isinstance(data, dict) and not data.get('source_unavailable'):
            region = region.upper()
            filtered = [r for r in data.get('regions', []) if r.get('REGIONID') == region]
            if not filtered:
                return cls._unavailable(f"region {region} not in live response",
                                        region=region)
            return {**data, 'regions': filtered,
                    'prices': [p for p in data.get('prices', []) if p.get('REGIONID') == region],
                    'count': len(filtered), 'region': region}
        return data


@global_power_bp.route('/api/power/global/zones')
def get_global_zones():
    """Get all available power zones across all APIs"""
    return jsonify({
        'success': True,
        'electricity_maps': {
            'zones': ElectricityMapsAPI.ZONES,
            'count': len(ElectricityMapsAPI.ZONES)
        },
        'uk_regions': {
            'regions': UKCarbonIntensityAPI.REGIONS,
            'count': len(UKCarbonIntensityAPI.REGIONS)
        },
        'entsoe_areas': ENTSOEAPI.get_areas(),
        'total_coverage': len(ElectricityMapsAPI.ZONES) + len(UKCarbonIntensityAPI.REGIONS) + len(ENTSOEAPI.AREAS)
    })


@global_power_bp.route('/api/power/carbon-intensity')
def get_carbon_intensity():
    """Get carbon intensity from multiple sources"""
    zone = request.args.get('zone', 'US-CAL-CISO')
    
    em_data = ElectricityMapsAPI.get_carbon_intensity(zone)
    
    uk_data = None
    if zone == 'GB' or zone.startswith('UK'):
        uk_data = UKCarbonIntensityAPI.get_current_intensity()
    
    return jsonify({
        'success': True,
        'zone': zone,
        'electricity_maps': em_data,
        'uk_national_grid': uk_data,
        'timestamp': datetime.now().isoformat()
    })


@global_power_bp.route('/api/power/uk/intensity')
def get_uk_intensity():
    """Get UK carbon intensity (national and regional)"""
    region_id = request.args.get('region', type=int)
    
    current = UKCarbonIntensityAPI.get_current_intensity()
    regional = UKCarbonIntensityAPI.get_regional_intensity(region_id)
    generation = UKCarbonIntensityAPI.get_generation_mix()
    
    return jsonify({
        'success': True,
        'national': current,
        'regional': regional if region_id else None,
        'all_regions': regional if not region_id else None,
        'generation_mix': generation,
        'regions_available': UKCarbonIntensityAPI.REGIONS,
        'timestamp': datetime.now().isoformat()
    })


@global_power_bp.route('/api/power/uk/forecast')
def get_uk_forecast():
    """Get UK carbon intensity forecast"""
    hours = request.args.get('hours', 24, type=int)
    forecast = UKCarbonIntensityAPI.get_forecast(hours)
    
    return jsonify({
        'success': True,
        'forecast': forecast,
        'hours': hours,
        'timestamp': datetime.now().isoformat()
    })


@global_power_bp.route('/api/power/europe/areas')
def get_europe_areas():
    """Get available European bidding zones"""
    return jsonify({
        'success': True,
        **ENTSOEAPI.get_areas()
    })


@global_power_bp.route('/api/power/europe/load')
def get_europe_load():
    """Get European grid load"""
    area = request.args.get('area', 'DE_LU')
    
    if area == 'all':
        loads = {}
        for area_code in ENTSOEAPI.AREAS:
            loads[area_code] = ENTSOEAPI.get_load(area_code)
        return jsonify({
            'success': True,
            'loads': loads,
            'count': len(loads)
        })
    
    return jsonify({
        'success': True,
        **ENTSOEAPI.get_load(area)
    })


@global_power_bp.route('/api/power/europe/generation')
def get_europe_generation():
    """Get European generation by type"""
    area = request.args.get('area', 'DE_LU')
    return jsonify({
        'success': True,
        **ENTSOEAPI.get_generation(area)
    })


@global_power_bp.route('/api/power/europe/prices')
def get_europe_prices():
    """Get European electricity prices"""
    area = request.args.get('area', 'DE_LU')
    
    if area == 'all':
        prices = {}
        for area_code in ENTSOEAPI.AREAS:
            prices[area_code] = ENTSOEAPI.get_prices(area_code)
        return jsonify({
            'success': True,
            'prices': prices,
            'count': len(prices)
        })
    
    return jsonify({
        'success': True,
        **ENTSOEAPI.get_prices(area)
    })


@global_power_bp.route('/api/power/europe/flows')
def get_europe_flows():
    """Get cross-border electricity flows"""
    area = request.args.get('area', 'DE_LU')
    return jsonify({
        'success': True,
        **ENTSOEAPI.get_cross_border_flows(area)
    })


@global_power_bp.route('/api/power/europe/gas')
def get_europe_gas():
    """Get European gas transmission operational data (ENTSOG, live/free).

    Honors ?indicator= (default 'Physical Flow') and ?limit= (default 100).
    Fails closed with source_unavailable when upstream is unreachable.
    """
    indicator = request.args.get('indicator', 'Physical Flow')
    limit = request.args.get('limit', 100, type=int)
    return jsonify({
        'success': True,
        **ENTSOGAPI.get_operational_data(indicator=indicator, limit=limit)
    })


@global_power_bp.route('/api/power/australia/nem')
def get_australia_nem():
    """Get AEMO NEM regional summary (live/free).

    Honors ?region= (NSW1|QLD1|SA1|TAS1|VIC1) to filter to one region.
    Fails closed with source_unavailable when upstream is unreachable.
    """
    region = request.args.get('region')
    return jsonify({
        'success': True,
        'regions_available': AEMONemAPI.REGIONS,
        **AEMONemAPI.get_nem_summary(region=region)
    })


@global_power_bp.route('/api/power/summary')
def get_power_summary():
    """Get summary across all power APIs"""
    
    uk = UKCarbonIntensityAPI.get_current_intensity()
    uk_gen = UKCarbonIntensityAPI.get_generation_mix()
    
    de = ENTSOEAPI.get_generation('DE_LU')
    fr = ENTSOEAPI.get_generation('FR')
    gb = ENTSOEAPI.get_generation('GB')
    
    return jsonify({
        'success': True,
        'summary': {
            'uk': {
                'carbon_intensity': uk.get('intensity', {}).get('actual') if uk else None,
                'index': uk.get('intensity', {}).get('index') if uk else None,
                'generation_mix': uk_gen.get('data', {}).get('generationmix') if uk_gen else None
            },
            'germany': {
                'total_generation_mw': de.get('total_generation_mw'),
                'renewable_percentage': de.get('renewable_percentage')
            },
            'france': {
                'total_generation_mw': fr.get('total_generation_mw'),
                'renewable_percentage': fr.get('renewable_percentage')
            },
            'great_britain': {
                'total_generation_mw': gb.get('total_generation_mw'),
                'renewable_percentage': gb.get('renewable_percentage')
            }
        },
        'coverage': {
            'electricity_maps_zones': len(ElectricityMapsAPI.ZONES),
            'uk_regions': len(UKCarbonIntensityAPI.REGIONS),
            'entsoe_areas': len(ENTSOEAPI.AREAS),
            'total': len(ElectricityMapsAPI.ZONES) + len(UKCarbonIntensityAPI.REGIONS) + len(ENTSOEAPI.AREAS)
        },
        'timestamp': datetime.now().isoformat()
    })


def register_global_power_routes(app):
    """Register global power API routes"""
    app.register_blueprint(global_power_bp)
    em_ok = bool(os.environ.get(ElectricityMapsAPI.TOKEN_ENV))
    entsoe_ok = bool(entsoe_token())   # r67: checks ENTSOE_API_TOKEN OR ENTSOE_TOKEN
    logger.info("✅ Global Power APIs registered")
    logger.info("   🌍 Electricity Maps: 30+ zones (token %s: %s)",
                ElectricityMapsAPI.TOKEN_ENV, "set" if em_ok else "MISSING → fail-closed")
    logger.info("   🇬🇧 UK Carbon Intensity: 14 regions (free)")
    logger.info("   🇪🇺 ENTSO-E: 23 European areas (token %s: %s)",
                ENTSOEAPI.TOKEN_ENV, "set" if entsoe_ok else "MISSING → fail-closed")
    logger.info("   🛢️ ENTSOG: EU gas transmission (free public API)")
    logger.info("   🇦🇺 AEMO NEM: 5 Australian regions (free public API)")
    print("⚡ Global Power APIs: ✅ Registered")
    print("   📍 /api/power/global/zones - All available zones")
    print("   📍 /api/power/carbon-intensity - Global carbon data (token-gated, fail-closed)")
    print("   📍 /api/power/uk/* - UK-specific endpoints (free)")
    print("   📍 /api/power/europe/* - European grid data (ENTSO-E token-gated, fail-closed)")
    print("   📍 /api/power/europe/gas - EU gas transmission (ENTSOG, free)")
    print("   📍 /api/power/australia/nem - AEMO NEM regions (free)")
    print("   📍 /api/power/summary - Multi-region summary")
