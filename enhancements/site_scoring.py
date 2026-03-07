"""
DC Hub - Enhanced Site Scoring Engine
Incorporates energy pricing, carbon intensity, and infrastructure proximity
Fixed import paths for enhancements/ subfolder
With 5-second timeout, 5-minute caching, and fallback support
"""

import os
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, asdict
from functools import lru_cache
import math
import hashlib
import time
import threading

# =============================================================================
# CACHING SYSTEM (5-minute TTL)
# =============================================================================

class SiteScoreCache:
    """Thread-safe cache with 5-minute TTL"""
    
    def __init__(self, ttl_seconds: int = 300):
        self._cache: Dict[str, Tuple[Any, float]] = {}
        self._lock = threading.Lock()
        self.ttl = ttl_seconds
    
    def _make_key(self, lat: float, lon: float, state: str) -> str:
        """Generate cache key from coordinates (rounded to 2 decimals)"""
        return f"{round(lat, 2)}:{round(lon, 2)}:{state.upper()}"
    
    def get(self, lat: float, lon: float, state: str) -> Optional[Any]:
        """Get cached value if not expired"""
        key = self._make_key(lat, lon, state)
        with self._lock:
            if key in self._cache:
                value, timestamp = self._cache[key]
                if time.time() - timestamp < self.ttl:
                    return value
                else:
                    del self._cache[key]
        return None
    
    def set(self, lat: float, lon: float, state: str, value: Any):
        """Cache a value"""
        key = self._make_key(lat, lon, state)
        with self._lock:
            self._cache[key] = (value, time.time())
    
    def clear_expired(self):
        """Remove expired entries"""
        now = time.time()
        with self._lock:
            expired = [k for k, (_, ts) in self._cache.items() if now - ts >= self.ttl]
            for k in expired:
                del self._cache[k]

# Global cache instance
_site_score_cache = SiteScoreCache(ttl_seconds=300)  # 5 minutes

# API timeout (5 seconds)
API_TIMEOUT = 5


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class SiteScore:
    """Comprehensive site score for data center location"""
    overall_score: float  # 0-100
    energy_score: float
    carbon_score: float
    infrastructure_score: float
    connectivity_score: float
    risk_score: float
    cost_score: float
    
    # Detailed breakdowns
    energy_details: Dict
    carbon_details: Dict
    infrastructure_details: Dict
    
    latitude: float
    longitude: float
    timestamp: str


# =============================================================================
# ENERGY PRICING DATA SOURCES
# =============================================================================

class EnergyPricingService:
    """
    Aggregate energy pricing data from multiple sources:
    - EIA (US Energy Information Administration)
    - GridStatus (real-time wholesale prices)
    - Utility rate databases
    """
    
    EIA_BASE_URL = "https://api.eia.gov/v2"
    
    def __init__(self, eia_api_key: Optional[str] = None):
        self.eia_key = eia_api_key or os.environ.get("EIA_API_KEY")
        self.session = requests.Session()
    
    def get_state_electricity_prices(self, state: str) -> Dict:
        """
        Get average electricity prices by state from EIA
        Returns $/kWh for commercial and industrial sectors
        """
        if not self.eia_key:
            return self._get_fallback_prices(state)
        
        try:
            url = f"{self.EIA_BASE_URL}/electricity/retail-sales/data"
            params = {
                'api_key': self.eia_key,
                'frequency': 'monthly',
                'data[0]': 'price',
                'facets[stateid][]': state.upper(),
                'sort[0][column]': 'period',
                'sort[0][direction]': 'desc',
                'length': 6
            }
            
            response = self.session.get(url, params=params, timeout=API_TIMEOUT)
            response.raise_for_status()
            data = response.json()
            
            return self._format_eia_prices(data, state)
        except Exception as e:
            return self._get_fallback_prices(state)
    
    def _get_fallback_prices(self, state: str) -> Dict:
        """Fallback prices based on 2024 EIA data"""
        # Average industrial electricity prices by state (cents/kWh)
        state_prices = {
            "TX": 7.23,   # Texas - very competitive
            "VA": 7.89,   # Virginia - data center hub
            "NV": 8.15,   # Nevada
            "AZ": 8.32,   # Arizona
            "GA": 8.56,   # Georgia
            "NC": 8.78,   # North Carolina
            "OH": 8.92,   # Ohio - PJM
            "IL": 9.12,   # Illinois
            "NY": 12.45,  # New York - higher
            "CA": 18.92,  # California - highest
            "WA": 5.89,   # Washington - hydro
            "OR": 6.23,   # Oregon - hydro
            "IA": 6.78,   # Iowa - wind
            "NE": 7.01,   # Nebraska
            "OK": 6.95,   # Oklahoma - wind
        }
        
        price = state_prices.get(state.upper(), 9.50)  # Default average
        
        return {
            "state": state.upper(),
            "industrial_price_cents_kwh": price,
            "industrial_price_dollars_kwh": price / 100,
            "commercial_price_cents_kwh": price * 1.15,  # Commercial ~15% higher
            "source": "EIA 2024 Historical Data",
            "data_quality": "fallback"
        }
    
    def _format_eia_prices(self, data: Dict, state: str) -> Dict:
        """Format EIA API response - parses all sector records"""
        if not data.get("response", {}).get("data"):
            return self._get_fallback_prices(state)
        
        records = data["response"]["data"]
        
        industrial = None
        commercial = None
        residential = None
        all_sectors = None
        latest_period = None
        price_trend = []
        
        for record in records:
            sector = record.get("sectorid")
            price_val = record.get("price")
            period = record.get("period")
            
            if price_val is None:
                continue
            
            price_float = float(price_val)
            
            if latest_period is None:
                latest_period = period
            
            if period == latest_period:
                if sector == "IND":
                    industrial = price_float
                elif sector == "COM":
                    commercial = price_float
                elif sector == "RES":
                    residential = price_float
                elif sector == "ALL":
                    all_sectors = price_float
            
            if sector == "IND" and price_val is not None:
                price_trend.append({"period": period, "price": price_float})
        
        fallback = self._get_fallback_prices(state)
        ind_price = industrial if industrial is not None else fallback["industrial_price_cents_kwh"]
        com_price = commercial if commercial is not None else (ind_price * 1.15)
        
        return {
            "state": state.upper(),
            "industrial_price_cents_kwh": float(ind_price),
            "industrial_price_dollars_kwh": float(ind_price) / 100,
            "commercial_price_cents_kwh": float(com_price),
            "residential_price_cents_kwh": float(residential) if residential is not None else None,
            "all_sectors_price_cents_kwh": float(all_sectors) if all_sectors is not None else None,
            "estimated_monthly_cost_per_mw": round(float(ind_price) * 1000 * 730 / 100, 0),
            "period": latest_period,
            "price_trend_12mo": price_trend[:12],
            "source": "EIA v2 API (Live)",
            "data_quality": "live"
        }
    
    def get_wholesale_price(self, iso: str, zone: Optional[str] = None) -> Dict:
        """Get real-time wholesale electricity prices (LMP)"""
        # Simplified - avoid circular import
        return {"error": "Use /api/grid/prices endpoint"}


# =============================================================================
# CARBON INTENSITY TRACKING
# =============================================================================

class CarbonIntensityService:
    """
    Track carbon intensity (gCO2/kWh) by grid region
    Sources: EPA eGRID, WattTime, ElectricityMaps
    """
    
    # EPA eGRID 2022 data - annual average gCO2/kWh by subregion
    EGRID_CARBON_INTENSITY = {
        # ERCOT
        "ERCT": 386,
        
        # PJM subregions
        "RFCE": 362,  # RFC East
        "RFCM": 489,  # RFC Michigan
        "RFCW": 518,  # RFC West
        
        # MISO
        "MROW": 517,  # MRO West
        "MROE": 589,  # MRO East
        
        # CAISO
        "CAMX": 234,
        
        # SPP
        "SPSO": 418,
        "SPNO": 442,
        
        # SERC
        "SRSO": 412,  # South
        "SRTV": 397,  # Tennessee Valley
        "SRMW": 645,  # Midwest
        "SRVC": 354,  # Virginia/Carolina
        
        # WECC
        "NWPP": 267,  # Northwest
        "RMPA": 547,  # Rocky Mountain
        "AZNM": 379,  # Arizona/New Mexico
        
        # NYISO
        "NYCW": 253,
        "NYUP": 156,
        
        # ISO-NE
        "NEWE": 267
    }
    
    # Mapping states to primary eGRID subregions
    STATE_TO_EGRID = {
        "TX": "ERCT",
        "CA": "CAMX",
        "VA": "SRVC",
        "NC": "SRVC",
        "GA": "SRSO",
        "AZ": "AZNM",
        "NV": "NWPP",
        "OH": "RFCW",
        "IL": "RFCW",
        "NY": "NYCW",
        "NJ": "RFCE",
        "PA": "RFCE",
        "WA": "NWPP",
        "OR": "NWPP",
        "IA": "MROW",
        "NE": "MROW",
        "OK": "SPSO",
    }
    
    def __init__(self, watttime_key: Optional[str] = None):
        self.watttime_key = watttime_key or os.environ.get("WATTTIME_API_KEY")
        self.session = requests.Session()
    
    def get_carbon_intensity(self, lat: float, lon: float) -> Dict:
        """
        Get carbon intensity for a location
        Tries WattTime API first, falls back to eGRID data
        """
        # Try WattTime for real-time data
        if self.watttime_key:
            watttime_data = self._get_watttime_intensity(lat, lon)
            if "error" not in watttime_data:
                return watttime_data
        
        # Fallback to eGRID regional averages
        return self._get_egrid_intensity(lat, lon)
    
    def get_carbon_intensity_by_state(self, state: str) -> Dict:
        """Get carbon intensity using state-level eGRID mapping"""
        subregion = self.STATE_TO_EGRID.get(state.upper())
        
        if subregion:
            intensity = self.EGRID_CARBON_INTENSITY.get(subregion, 400)
            return {
                "state": state.upper(),
                "egrid_subregion": subregion,
                "carbon_intensity_gco2_kwh": intensity,
                "source": "EPA eGRID 2022",
                "rating": self._get_carbon_rating(intensity)
            }
        
        return {
            "state": state.upper(),
            "carbon_intensity_gco2_kwh": 400,  # US average
            "source": "EPA eGRID 2022 (US Average)",
            "rating": "average"
        }
    
    def _get_watttime_intensity(self, lat: float, lon: float) -> Dict:
        """Get real-time carbon intensity from WattTime"""
        try:
            # WattTime requires authentication
            auth_url = "https://api.watttime.org/login"
            auth_response = self.session.get(auth_url, auth=(self.watttime_key, ""))
            
            if auth_response.status_code != 200:
                return {"error": "WattTime auth failed"}
            
            token = auth_response.json().get("token")
            
            # Get real-time intensity
            url = "https://api.watttime.org/v3/signal-index"
            headers = {"Authorization": f"Bearer {token}"}
            params = {"latitude": lat, "longitude": lon}
            
            response = self.session.get(url, headers=headers, params=params, timeout=API_TIMEOUT)
            data = response.json()
            
            return {
                "latitude": lat,
                "longitude": lon,
                "carbon_intensity_gco2_kwh": data.get("value"),
                "moer": data.get("moer"),  # Marginal Operating Emissions Rate
                "source": "WattTime Real-time",
                "timestamp": data.get("point_time")
            }
        except Exception as e:
            return {"error": str(e)}
    
    def _get_egrid_intensity(self, lat: float, lon: float) -> Dict:
        """Get carbon intensity from eGRID based on location"""
        # Simple region detection based on coordinates
        
        if lat > 42 and lon < -70:  # New England
            subregion = "NEWE"
        elif lat > 40 and lon > -80:  # NY/NJ area
            subregion = "NYCW"
        elif lat > 35 and lat < 42 and lon > -90:  # PJM region
            subregion = "RFCE"
        elif lat > 25 and lat < 37 and lon > -100 and lon < -93:  # Texas
            subregion = "ERCT"
        elif lat > 32 and lat < 42 and lon < -115:  # California
            subregion = "CAMX"
        elif lat > 42 and lon < -115:  # Pacific Northwest
            subregion = "NWPP"
        else:
            subregion = "RFCW"  # Default to RFC West (Midwest)
        
        intensity = self.EGRID_CARBON_INTENSITY.get(subregion, 400)
        
        return {
            "latitude": lat,
            "longitude": lon,
            "egrid_subregion": subregion,
            "carbon_intensity_gco2_kwh": intensity,
            "source": "EPA eGRID 2022",
            "rating": self._get_carbon_rating(intensity)
        }
    
    def _get_carbon_rating(self, intensity: float) -> str:
        """Rate carbon intensity on a scale"""
        if intensity < 200:
            return "excellent"
        elif intensity < 300:
            return "good"
        elif intensity < 400:
            return "average"
        elif intensity < 500:
            return "below_average"
        else:
            return "poor"


# =============================================================================
# SITE SCORING ENGINE
# =============================================================================

class SiteScoringEngine:
    """
    Comprehensive site scoring for data center locations
    Weighs multiple factors to produce overall score
    """
    
    # Scoring weights (must sum to 100)
    WEIGHTS = {
        "energy": 25,      # Energy cost and availability
        "carbon": 20,      # Carbon intensity
        "infrastructure": 20,  # Power infrastructure proximity
        "connectivity": 15,    # Network/fiber connectivity
        "risk": 10,        # Natural disaster and climate risk
        "cost": 10         # Real estate and labor costs
    }
    
    def __init__(self):
        self.energy_service = EnergyPricingService()
        self.carbon_service = CarbonIntensityService()
    
    def calculate_site_score(
        self,
        lat: float,
        lon: float,
        state: str,
        nearby_substations: List[Dict] = None,
        nearby_fiber: List[Dict] = None
    ) -> SiteScore:
        """
        Calculate comprehensive site score
        """
        
        # Get energy pricing data
        energy_data = self.energy_service.get_state_electricity_prices(state)
        energy_score, energy_details = self._score_energy(energy_data)
        
        # Get carbon intensity
        carbon_data = self.carbon_service.get_carbon_intensity(lat, lon)
        carbon_score, carbon_details = self._score_carbon(carbon_data)
        
        # Score infrastructure proximity
        infra_score, infra_details = self._score_infrastructure(
            nearby_substations or [],
            nearby_fiber or []
        )
        
        # Score connectivity
        connectivity_score = self._score_connectivity(lat, lon)
        
        # Score risk factors
        risk_score = self._score_risk(lat, lon, state)
        
        # Score cost factors
        cost_score = self._score_cost(state)
        
        # Calculate weighted overall score
        overall = (
            energy_score * self.WEIGHTS["energy"] +
            carbon_score * self.WEIGHTS["carbon"] +
            infra_score * self.WEIGHTS["infrastructure"] +
            connectivity_score * self.WEIGHTS["connectivity"] +
            risk_score * self.WEIGHTS["risk"] +
            cost_score * self.WEIGHTS["cost"]
        ) / 100
        
        return SiteScore(
            overall_score=round(overall, 1),
            energy_score=round(energy_score, 1),
            carbon_score=round(carbon_score, 1),
            infrastructure_score=round(infra_score, 1),
            connectivity_score=round(connectivity_score, 1),
            risk_score=round(risk_score, 1),
            cost_score=round(cost_score, 1),
            energy_details=energy_details,
            carbon_details=carbon_details,
            infrastructure_details=infra_details,
            latitude=lat,
            longitude=lon,
            timestamp=datetime.utcnow().isoformat()
        )
    
    def _score_energy(self, energy_data: Dict) -> Tuple[float, Dict]:
        """Score based on energy costs (0-100, higher is better)"""
        price = energy_data.get("industrial_price_cents_kwh", 10)
        
        # Score: 100 at $0.05/kWh, 0 at $0.20/kWh
        score = max(0, min(100, (20 - price) / 15 * 100))
        
        details = {
            "industrial_price_cents_kwh": price,
            "price_rating": self._get_price_rating(price),
            "estimated_monthly_cost_per_mw": round(price * 1000 * 730 / 100, 0),
            "source": energy_data.get("source")
        }
        
        return score, details
    
    def _score_carbon(self, carbon_data: Dict) -> Tuple[float, Dict]:
        """Score based on carbon intensity (0-100, higher is better)"""
        intensity = carbon_data.get("carbon_intensity_gco2_kwh", 400)
        
        # Score: 100 at 100 gCO2/kWh, 0 at 700 gCO2/kWh
        score = max(0, min(100, (700 - intensity) / 6))
        
        details = {
            "carbon_intensity_gco2_kwh": intensity,
            "rating": carbon_data.get("rating", "unknown"),
            "egrid_subregion": carbon_data.get("egrid_subregion"),
            "source": carbon_data.get("source")
        }
        
        return score, details
    
    def _score_infrastructure(
        self,
        substations: List[Dict],
        fiber: List[Dict]
    ) -> Tuple[float, Dict]:
        """Score based on proximity to power and fiber infrastructure"""
        
        sub_score = 0
        closest_substation = None
        high_voltage_nearby = False
        
        for sub in substations:
            distance_km = sub.get("distance_km", 999)
            voltage = sub.get("voltage_kv", 0)
            
            if closest_substation is None or distance_km < closest_substation["distance_km"]:
                closest_substation = sub
            
            if voltage >= 230:
                high_voltage_nearby = True
            
            if distance_km <= 5:
                sub_score = max(sub_score, 50)
            elif distance_km <= 50:
                sub_score = max(sub_score, 50 * (1 - (distance_km - 5) / 45))
        
        if high_voltage_nearby:
            sub_score += 25
        
        fiber_score = 0
        closest_fiber = None
        
        for f in fiber:
            distance_km = f.get("distance_km", 999)
            
            if closest_fiber is None or distance_km < closest_fiber["distance_km"]:
                closest_fiber = f
            
            if distance_km <= 1:
                fiber_score = max(fiber_score, 25)
            elif distance_km <= 10:
                fiber_score = max(fiber_score, 25 * (1 - (distance_km - 1) / 9))
        
        # Default scores if no infrastructure data provided
        if not substations:
            sub_score = 50  # Assume average
        if not fiber:
            fiber_score = 15  # Assume average
        
        total_score = min(100, sub_score + fiber_score)
        
        details = {
            "closest_substation": closest_substation,
            "high_voltage_nearby": high_voltage_nearby,
            "closest_fiber": closest_fiber,
            "substations_within_10km": len([s for s in substations if s.get("distance_km", 999) <= 10]),
            "substation_score": round(sub_score, 1),
            "fiber_score": round(fiber_score, 1)
        }
        
        return total_score, details
    
    def _score_connectivity(self, lat: float, lon: float) -> float:
        """Score network connectivity"""
        major_markets = [
            (39.0438, -77.4874, "Ashburn"),
            (37.7749, -122.4194, "SF Bay"),
            (34.0522, -118.2437, "Los Angeles"),
            (32.7767, -96.7970, "Dallas"),
            (41.8781, -87.6298, "Chicago"),
            (40.7128, -74.0060, "NYC/NJ"),
            (47.6062, -122.3321, "Seattle"),
            (33.4484, -112.0740, "Phoenix"),
            (36.1699, -115.1398, "Las Vegas"),
            (33.7490, -84.3880, "Atlanta")
        ]
        
        min_distance = float('inf')
        for market_lat, market_lon, _ in major_markets:
            dist = self._haversine_distance(lat, lon, market_lat, market_lon)
            min_distance = min(min_distance, dist)
        
        if min_distance <= 50:
            return 100
        elif min_distance <= 100:
            return 90
        elif min_distance <= 250:
            return 70
        elif min_distance <= 500:
            return 50
        else:
            return max(20, 100 - min_distance / 10)
    
    def _score_risk(self, lat: float, lon: float, state: str) -> float:
        """Score natural disaster and climate risk"""
        state_risk_scores = {
            "VA": 85, "OH": 80, "TX": 60, "CA": 55, "FL": 50,
            "AZ": 75, "NV": 80, "GA": 70, "NC": 70, "NY": 75,
            "NJ": 75, "WA": 70, "OR": 70,
        }
        return state_risk_scores.get(state.upper(), 65)
    
    def _score_cost(self, state: str) -> float:
        """Score overall cost factors"""
        cost_scores = {
            "TX": 85, "AZ": 80, "NV": 75, "GA": 80, "NC": 75,
            "OH": 80, "VA": 65, "CA": 35, "NY": 40, "NJ": 50,
            "WA": 60, "OR": 65,
        }
        return cost_scores.get(state.upper(), 70)
    
    def _get_price_rating(self, price_cents: float) -> str:
        """Rate electricity price"""
        if price_cents < 6:
            return "excellent"
        elif price_cents < 8:
            return "good"
        elif price_cents < 10:
            return "average"
        elif price_cents < 12:
            return "above_average"
        else:
            return "expensive"
    
    def _haversine_distance(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate distance between two points in km"""
        R = 6371
        
        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        delta_lat = math.radians(lat2 - lat1)
        delta_lon = math.radians(lon2 - lon1)
        
        a = math.sin(delta_lat/2)**2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon/2)**2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
        
        return R * c


# =============================================================================
# FLASK ROUTES
# =============================================================================

def register_scoring_routes(app):
    """Register site scoring routes with Flask app"""
    from flask import jsonify, request
    from dataclasses import asdict
    from api_tier_gating import require_plan
    
    scoring_engine = SiteScoringEngine()
    energy_service = EnergyPricingService()
    carbon_service = CarbonIntensityService()
    
    @app.route('/api/site-score', methods=['GET', 'POST'])
    @require_plan('pro')
    def calculate_site_score():
        """Calculate site score for a location (with 5-min caching and 5s timeout)"""
        if request.method == 'POST':
            data = request.json or {}
        else:
            data = request.args
        
        try:
            lat = float(data.get('lat', 0))
            lon = float(data.get('lon', 0)) if data.get('lon') else float(data.get('lng', 0))
            state = data.get('state', 'TX')
            substations = data.get('substations', [])
            fiber = data.get('fiber', [])
            
            # Check cache first (5-minute TTL)
            cached = _site_score_cache.get(lat, lon, state)
            if cached:
                cached['cached'] = True
                return jsonify(cached)
            
            # Calculate score with timeout protection
            score = scoring_engine.calculate_site_score(
                lat, lon, state, substations, fiber
            )
            
            result = asdict(score)
            result['cached'] = False
            
            # Cache the result
            _site_score_cache.set(lat, lon, state, result)
            
            return jsonify(result)
        except Exception as e:
            import traceback
            return jsonify({"success": False, "error": str(e), "trace": traceback.format_exc()[-500:]}), 500
    
    @app.route('/api/energy/prices/<state>', methods=['GET'])
    @require_plan('pro')
    def get_energy_prices(state):
        """Get energy prices for a state"""
        result = energy_service.get_state_electricity_prices(state)
        return jsonify(result)
    
    @app.route('/api/carbon/intensity', methods=['GET'])
    @require_plan('pro')
    def get_carbon_intensity():
        """Get carbon intensity for a location"""
        lat = request.args.get('lat')
        lon = request.args.get('lon')
        state = request.args.get('state')
        
        if lat and lon:
            result = carbon_service.get_carbon_intensity(float(lat), float(lon))
        elif state:
            result = carbon_service.get_carbon_intensity_by_state(state)
        else:
            return jsonify({"error": "Provide lat/lon or state"}), 400
        
        return jsonify(result)
    
    @app.route('/api/site-score/batch', methods=['POST'])
    @require_plan('enterprise')
    def batch_site_scores():
        """Calculate scores for multiple locations"""
        data = request.json or {}
        locations = data.get('locations', [])
        
        results = []
        for loc in locations[:20]:
            try:
                score = scoring_engine.calculate_site_score(
                    loc.get('lat'),
                    loc.get('lon'),
                    loc.get('state'),
                    loc.get('substations', []),
                    loc.get('fiber', [])
                )
                results.append(asdict(score))
            except:
                pass
        
        return jsonify({"scores": results, "count": len(results)})
    
    return app
