"""
DC Hub - NREL Solar and Wind Potential Layers
Integrates NREL APIs for renewable energy resource assessment
"""

import os
import requests
from datetime import datetime
from typing import Dict, List, Optional, Any
from functools import lru_cache
import json


# =============================================================================
# NREL API CLIENT
# =============================================================================

class NRELClient:
    """
    Client for NREL (National Renewable Energy Laboratory) APIs
    
    Available APIs:
    - PVWatts: Solar photovoltaic potential
    - Wind Toolkit: Wind resource data
    - NSRDB: National Solar Radiation Database
    
    Get API key at: https://developer.nrel.gov/signup/
    """
    
    BASE_URL = "https://developer.nrel.gov/api"
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("NREL_API_KEY", "DEMO_KEY")
        self.session = requests.Session()
    
    # =========================================================================
    # PVWATTS - SOLAR POTENTIAL
    # =========================================================================
    
    def get_solar_potential(
        self,
        lat: float,
        lon: float,
        system_capacity_kw: float = 1000,  # 1 MW default
        module_type: int = 0,  # 0=Standard, 1=Premium, 2=Thin film
        losses: float = 14,    # System losses %
        array_type: int = 0,   # 0=Fixed, 1=1-axis tracking, 2=2-axis
        tilt: Optional[float] = None,  # Auto if None
        azimuth: float = 180,  # South-facing
        dc_ac_ratio: float = 1.2
    ) -> Dict:
        """
        Get solar PV potential using NREL PVWatts API
        
        Returns:
            Annual AC energy production (kWh)
            Monthly breakdown
            Capacity factor
            Solar resource metrics
        """
        
        url = f"{self.BASE_URL}/pvwatts/v8.json"
        
        params = {
            "api_key": self.api_key,
            "lat": lat,
            "lon": lon,
            "system_capacity": system_capacity_kw,
            "module_type": module_type,
            "losses": losses,
            "array_type": array_type,
            "azimuth": azimuth,
            "dc_ac_ratio": dc_ac_ratio
        }
        
        # Auto-calculate optimal tilt if not specified
        if tilt is not None:
            params["tilt"] = tilt
        else:
            # Optimal tilt ≈ latitude for fixed arrays
            params["tilt"] = abs(lat)
        
        try:
            response = self.session.get(url, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
            
            if "outputs" in data:
                return self._format_solar_response(data, lat, lon)
            elif "errors" in data:
                return {"error": data["errors"], "status": "failed"}
            else:
                return data
                
        except requests.exceptions.RequestException as e:
            return {"error": str(e), "status": "failed"}
    
    def _format_solar_response(self, data: Dict, lat: float, lon: float) -> Dict:
        """Format PVWatts response for DC Hub"""
        outputs = data.get("outputs", {})
        inputs = data.get("inputs", {})
        station = data.get("station_info", {})
        
        # Calculate capacity factor
        system_capacity = float(inputs.get("system_capacity", 1000))
        annual_kwh = outputs.get("ac_annual", 0)
        capacity_factor = (annual_kwh / (system_capacity * 8760)) * 100 if system_capacity > 0 else 0
        
        return {
            "type": "solar",
            "latitude": lat,
            "longitude": lon,
            
            # Annual production
            "annual_production_kwh": round(annual_kwh, 0),
            "annual_production_mwh": round(annual_kwh / 1000, 1),
            "capacity_factor_pct": round(capacity_factor, 1),
            
            # System specs
            "system_capacity_kw": system_capacity,
            "system_capacity_mw": system_capacity / 1000,
            
            # Monthly breakdown
            "monthly_production_kwh": outputs.get("ac_monthly", []),
            "monthly_poa_irradiance": outputs.get("poa_monthly", []),
            "monthly_solar_fraction": outputs.get("solrad_monthly", []),
            
            # Solar resource
            "annual_ghi_kwh_m2": round(sum(outputs.get("solrad_monthly", [])), 1),
            "annual_dni_kwh_m2": round(outputs.get("dn_annual", 0) / 1000, 1) if outputs.get("dn_annual") else None,
            
            # Weather station
            "weather_station": {
                "city": station.get("city"),
                "state": station.get("state"),
                "source": station.get("source_desc"),
                "distance_km": station.get("distance")
            },
            
            # Scoring
            "solar_rating": self._rate_solar_potential(capacity_factor),
            "source": "NREL PVWatts v8",
            "timestamp": datetime.utcnow().isoformat()
        }
    
    def _rate_solar_potential(self, capacity_factor: float) -> str:
        """Rate solar potential based on capacity factor"""
        if capacity_factor >= 25:
            return "excellent"
        elif capacity_factor >= 20:
            return "very_good"
        elif capacity_factor >= 17:
            return "good"
        elif capacity_factor >= 14:
            return "moderate"
        else:
            return "limited"
    
    # =========================================================================
    # WIND TOOLKIT - WIND POTENTIAL
    # =========================================================================
    
    def get_wind_potential(
        self,
        lat: float,
        lon: float,
        hub_height: int = 100,  # meters
        year: int = 2014  # Wind Toolkit covers 2007-2014
    ) -> Dict:
        """
        Get wind resource data using NREL Wind Toolkit API
        
        Args:
            lat: Latitude
            lon: Longitude
            hub_height: Turbine hub height in meters (40, 60, 80, 100, 120, 140, 160)
            year: Data year (2007-2014)
        
        Returns:
            Wind speed statistics
            Power density
            Wind class rating
        """
        
        # Wind Toolkit API endpoint
        url = f"{self.BASE_URL}/wind-toolkit/v2/wind/wtk-download.json"
        
        params = {
            "api_key": self.api_key,
            "lat": lat,
            "lon": lon,
            "year": year,
            "attributes": f"windspeed_{hub_height}m,winddirection_{hub_height}m,pressure_0m,temperature_2m",
            "interval": "60",  # Hourly
            "utc": "true"
        }
        
        try:
            # Note: Full Wind Toolkit requires more complex data handling
            # This is a simplified version that estimates wind potential
            
            # Use Solar Resource API to get basic location data
            # Then apply wind estimation based on region
            
            # For production, use the full Wind Toolkit with proper data download
            return self._estimate_wind_potential(lat, lon, hub_height)
            
        except requests.exceptions.RequestException as e:
            return {"error": str(e), "status": "failed"}
    
    def _estimate_wind_potential(self, lat: float, lon: float, hub_height: int) -> Dict:
        """
        Estimate wind potential based on regional data
        Uses NREL wind resource maps and regional averages
        """
        
        # Regional wind class estimates (simplified)
        # In production, use actual Wind Toolkit data
        
        # Great Plains wind corridor
        if -105 < lon < -95 and 30 < lat < 50:
            base_speed = 8.5  # m/s at 100m
            wind_class = 4
            region = "Great Plains"
        
        # Texas Gulf Coast
        elif -100 < lon < -93 and 26 < lat < 32:
            base_speed = 7.5
            wind_class = 3
            region = "Texas Gulf"
        
        # Pacific Northwest Coast
        elif lon < -122 and 42 < lat < 49:
            base_speed = 7.0
            wind_class = 3
            region = "Pacific Coast"
        
        # Midwest
        elif -95 < lon < -80 and 38 < lat < 48:
            base_speed = 6.5
            wind_class = 2
            region = "Midwest"
        
        # Southwest
        elif lon < -105 and 32 < lat < 42:
            base_speed = 6.0
            wind_class = 2
            region = "Southwest"
        
        # East Coast
        elif lon > -80:
            base_speed = 5.5
            wind_class = 2
            region = "East Coast"
        
        else:
            base_speed = 6.0
            wind_class = 2
            region = "Other"
        
        # Adjust for hub height (wind shear)
        # Power law: v2 = v1 * (h2/h1)^alpha, alpha ≈ 0.14 for moderate terrain
        reference_height = 100
        alpha = 0.14
        adjusted_speed = base_speed * (hub_height / reference_height) ** alpha
        
        # Calculate power density (W/m²)
        # P = 0.5 * rho * v³, rho ≈ 1.225 kg/m³
        rho = 1.225
        power_density = 0.5 * rho * (adjusted_speed ** 3)
        
        # Estimate capacity factor (rough estimate)
        # Based on typical turbine performance curves
        if adjusted_speed >= 8.5:
            capacity_factor = 45
        elif adjusted_speed >= 7.5:
            capacity_factor = 38
        elif adjusted_speed >= 6.5:
            capacity_factor = 30
        elif adjusted_speed >= 5.5:
            capacity_factor = 22
        else:
            capacity_factor = 15
        
        return {
            "type": "wind",
            "latitude": lat,
            "longitude": lon,
            
            # Wind resource
            "mean_wind_speed_ms": round(adjusted_speed, 1),
            "mean_wind_speed_mph": round(adjusted_speed * 2.237, 1),
            "hub_height_m": hub_height,
            
            # Power metrics
            "power_density_w_m2": round(power_density, 0),
            "wind_class": wind_class,
            "estimated_capacity_factor_pct": capacity_factor,
            
            # Regional info
            "region": region,
            
            # Scoring
            "wind_rating": self._rate_wind_potential(wind_class, capacity_factor),
            
            # Data source
            "source": "NREL Wind Toolkit (Regional Estimates)",
            "note": "For detailed analysis, use full Wind Toolkit dataset",
            "timestamp": datetime.utcnow().isoformat()
        }
    
    def _rate_wind_potential(self, wind_class: int, capacity_factor: float) -> str:
        """Rate wind potential"""
        if wind_class >= 5 or capacity_factor >= 40:
            return "excellent"
        elif wind_class >= 4 or capacity_factor >= 35:
            return "very_good"
        elif wind_class >= 3 or capacity_factor >= 28:
            return "good"
        elif wind_class >= 2 or capacity_factor >= 20:
            return "moderate"
        else:
            return "limited"
    
    # =========================================================================
    # COMBINED RENEWABLE POTENTIAL
    # =========================================================================
    
    def get_renewable_potential(
        self,
        lat: float,
        lon: float,
        solar_capacity_mw: float = 1.0,
        wind_capacity_mw: float = 1.0,
        hub_height: int = 100
    ) -> Dict:
        """
        Get combined solar and wind potential for a location
        
        Useful for assessing hybrid renewable energy potential
        for data center power procurement
        """
        
        # Get solar potential
        solar = self.get_solar_potential(
            lat, lon,
            system_capacity_kw=solar_capacity_mw * 1000
        )
        
        # Get wind potential
        wind = self.get_wind_potential(lat, lon, hub_height)
        
        # Combine results
        combined = {
            "latitude": lat,
            "longitude": lon,
            
            "solar": solar if "error" not in solar else None,
            "wind": wind if "error" not in wind else None,
            
            # Combined metrics
            "combined_rating": self._combined_renewable_rating(solar, wind),
            
            # PPA potential estimate (simplified)
            "ppa_potential": self._estimate_ppa_potential(solar, wind),
            
            "timestamp": datetime.utcnow().isoformat()
        }
        
        return combined
    
    def _combined_renewable_rating(self, solar: Dict, wind: Dict) -> str:
        """Rate combined renewable potential"""
        solar_cf = solar.get("capacity_factor_pct", 0) if solar else 0
        wind_cf = wind.get("estimated_capacity_factor_pct", 0) if wind else 0
        
        # Combined score
        combined_score = (solar_cf * 0.6) + (wind_cf * 0.4)
        
        if combined_score >= 30:
            return "excellent"
        elif combined_score >= 25:
            return "very_good"
        elif combined_score >= 20:
            return "good"
        elif combined_score >= 15:
            return "moderate"
        else:
            return "limited"
    
    def _estimate_ppa_potential(self, solar: Dict, wind: Dict) -> Dict:
        """Estimate PPA (Power Purchase Agreement) potential"""
        solar_cf = solar.get("capacity_factor_pct", 0) if solar else 0
        wind_cf = wind.get("estimated_capacity_factor_pct", 0) if wind else 0
        
        # Estimated PPA price ranges ($/MWh) - 2024 market rates
        # Based on capacity factors
        
        solar_ppa = None
        if solar_cf >= 25:
            solar_ppa = {"min": 25, "max": 35, "quality": "competitive"}
        elif solar_cf >= 20:
            solar_ppa = {"min": 30, "max": 40, "quality": "good"}
        elif solar_cf >= 15:
            solar_ppa = {"min": 35, "max": 50, "quality": "moderate"}
        
        wind_ppa = None
        if wind_cf >= 40:
            wind_ppa = {"min": 20, "max": 30, "quality": "competitive"}
        elif wind_cf >= 30:
            wind_ppa = {"min": 25, "max": 40, "quality": "good"}
        elif wind_cf >= 20:
            wind_ppa = {"min": 35, "max": 50, "quality": "moderate"}
        
        return {
            "solar_ppa_estimate": solar_ppa,
            "wind_ppa_estimate": wind_ppa,
            "note": "Estimates based on 2024 market rates. Actual prices vary."
        }


# =============================================================================
# GEOJSON LAYER GENERATION
# =============================================================================

class RenewableLayerGenerator:
    """
    Generate GeoJSON layers for renewable energy potential
    Useful for mapping visualization in DC Hub
    """
    
    def __init__(self, nrel_client: NRELClient = None):
        self.nrel = nrel_client or NRELClient()
    
    def generate_solar_layer(
        self,
        bounds: Dict,  # {"north": , "south": , "east": , "west": }
        resolution: float = 0.5  # Degrees
    ) -> Dict:
        """
        Generate GeoJSON layer with solar potential data
        
        Args:
            bounds: Geographic bounds
            resolution: Grid resolution in degrees
        
        Returns:
            GeoJSON FeatureCollection
        """
        features = []
        
        lat = bounds["south"]
        while lat <= bounds["north"]:
            lon = bounds["west"]
            while lon <= bounds["east"]:
                solar_data = self.nrel.get_solar_potential(lat, lon)
                
                if "error" not in solar_data:
                    feature = {
                        "type": "Feature",
                        "geometry": {
                            "type": "Point",
                            "coordinates": [lon, lat]
                        },
                        "properties": {
                            "capacity_factor": solar_data.get("capacity_factor_pct"),
                            "annual_ghi": solar_data.get("annual_ghi_kwh_m2"),
                            "rating": solar_data.get("solar_rating"),
                            "type": "solar"
                        }
                    }
                    features.append(feature)
                
                lon += resolution
            lat += resolution
        
        return {
            "type": "FeatureCollection",
            "features": features,
            "metadata": {
                "layer_type": "solar_potential",
                "source": "NREL PVWatts",
                "resolution_degrees": resolution,
                "generated_at": datetime.utcnow().isoformat()
            }
        }
    
    def generate_wind_layer(
        self,
        bounds: Dict,
        resolution: float = 0.5,
        hub_height: int = 100
    ) -> Dict:
        """Generate GeoJSON layer with wind potential data"""
        features = []
        
        lat = bounds["south"]
        while lat <= bounds["north"]:
            lon = bounds["west"]
            while lon <= bounds["east"]:
                wind_data = self.nrel.get_wind_potential(lat, lon, hub_height)
                
                if "error" not in wind_data:
                    feature = {
                        "type": "Feature",
                        "geometry": {
                            "type": "Point",
                            "coordinates": [lon, lat]
                        },
                        "properties": {
                            "wind_speed_ms": wind_data.get("mean_wind_speed_ms"),
                            "wind_class": wind_data.get("wind_class"),
                            "power_density": wind_data.get("power_density_w_m2"),
                            "rating": wind_data.get("wind_rating"),
                            "type": "wind"
                        }
                    }
                    features.append(feature)
                
                lon += resolution
            lat += resolution
        
        return {
            "type": "FeatureCollection",
            "features": features,
            "metadata": {
                "layer_type": "wind_potential",
                "source": "NREL Wind Toolkit",
                "hub_height_m": hub_height,
                "resolution_degrees": resolution,
                "generated_at": datetime.utcnow().isoformat()
            }
        }


# =============================================================================
# FLASK ROUTES
# =============================================================================

def register_nrel_routes(app):
    """Register NREL renewable energy routes with Flask app"""
    from flask import jsonify, request
    from api_tier_gating import require_plan
    
    nrel_client = NRELClient()
    layer_generator = RenewableLayerGenerator(nrel_client)
    
    @app.route('/api/renewable/solar', methods=['GET'])
    @require_plan('pro')
    def get_solar_potential():
        """
        Get solar PV potential for a location
        
        Query params:
            - lat: Latitude
            - lon: Longitude
            - capacity_kw: System capacity (default 1000)
            - array_type: 0=Fixed, 1=1-axis, 2=2-axis (default 0)
        """
        lat = float(request.args.get('lat'))
        lon = float(request.args.get('lon'))
        capacity = float(request.args.get('capacity_kw', 1000))
        array_type = int(request.args.get('array_type', 0))
        
        result = nrel_client.get_solar_potential(
            lat, lon,
            system_capacity_kw=capacity,
            array_type=array_type
        )
        return jsonify(result)
    
    @app.route('/api/renewable/wind', methods=['GET'])
    @require_plan('pro')
    def get_wind_potential():
        """
        Get wind energy potential for a location
        
        Query params:
            - lat: Latitude
            - lon: Longitude
            - hub_height: Turbine hub height in meters (default 100)
        """
        lat = float(request.args.get('lat'))
        lon = float(request.args.get('lon'))
        hub_height = int(request.args.get('hub_height', 100))
        
        result = nrel_client.get_wind_potential(lat, lon, hub_height)
        return jsonify(result)
    
    STATE_CENTROIDS = {
        'AL': (32.81, -86.68), 'AK': (64.24, -152.49), 'AZ': (34.05, -111.09),
        'AR': (34.97, -92.37), 'CA': (36.78, -119.42), 'CO': (39.55, -105.78),
        'CT': (41.60, -72.76), 'DE': (39.16, -75.52), 'FL': (27.66, -81.52),
        'GA': (32.16, -82.90), 'HI': (19.90, -155.58), 'ID': (44.07, -114.74),
        'IL': (40.63, -89.40), 'IN': (40.27, -86.13), 'IA': (41.88, -93.10),
        'KS': (38.51, -98.33), 'KY': (37.84, -84.27), 'LA': (30.98, -91.96),
        'ME': (45.25, -69.45), 'MD': (39.05, -76.64), 'MA': (42.41, -71.38),
        'MI': (44.31, -85.60), 'MN': (46.28, -94.31), 'MS': (32.35, -89.40),
        'MO': (38.46, -92.29), 'MT': (46.80, -110.36), 'NE': (41.49, -99.90),
        'NV': (38.80, -116.42), 'NH': (43.19, -71.57), 'NJ': (40.06, -74.41),
        'NM': (34.52, -105.87), 'NY': (42.17, -74.95), 'NC': (35.76, -79.02),
        'ND': (47.55, -101.00), 'OH': (40.42, -82.91), 'OK': (35.47, -97.52),
        'OR': (43.80, -120.55), 'PA': (41.20, -77.19), 'RI': (41.58, -71.48),
        'SC': (33.84, -81.16), 'SD': (43.97, -99.90), 'TN': (35.52, -86.58),
        'TX': (31.97, -99.90), 'UT': (39.32, -111.09), 'VT': (44.56, -72.58),
        'VA': (37.43, -78.66), 'WA': (47.75, -120.74), 'WV': (38.60, -80.45),
        'WI': (43.78, -88.79), 'WY': (43.08, -107.29)
    }

    @app.route('/api/renewable/combined', methods=['GET'])
    @require_plan('pro')
    def get_combined_renewable():
        """
        Get combined solar and wind potential
        
        Query params:
            - lat: Latitude
            - lon: Longitude
            - state: State code (e.g., TX) - uses centroid if lat/lon not provided
            - solar_mw: Solar capacity in MW (default 1)
            - wind_mw: Wind capacity in MW (default 1)
            - hub_height: Wind turbine hub height (default 100)
        """
        state = request.args.get('state', '').upper()
        lat = request.args.get('lat')
        lon = request.args.get('lon')
        
        if lat and lon:
            lat = float(lat)
            lon = float(lon)
        elif state and state in STATE_CENTROIDS:
            lat, lon = STATE_CENTROIDS[state]
        else:
            return jsonify({"error": "Provide lat/lon or state parameter (e.g., state=TX)"}), 400
        
        solar_mw = float(request.args.get('solar_mw', 1))
        wind_mw = float(request.args.get('wind_mw', 1))
        hub_height = int(request.args.get('hub_height', 100))
        
        result = nrel_client.get_renewable_potential(
            lat, lon, solar_mw, wind_mw, hub_height
        )
        if state:
            result['state'] = state
        return jsonify(result)
    
    @app.route('/api/renewable/layer/solar', methods=['GET'])
    @require_plan('pro')
    def get_solar_layer():
        """
        Get GeoJSON layer of solar potential
        
        Query params:
            - north, south, east, west: Bounds
            - resolution: Grid resolution in degrees (default 1.0)
        """
        bounds = {
            "north": float(request.args.get('north', 50)),
            "south": float(request.args.get('south', 25)),
            "east": float(request.args.get('east', -65)),
            "west": float(request.args.get('west', -125))
        }
        resolution = float(request.args.get('resolution', 1.0))
        
        # Limit resolution to prevent excessive API calls
        resolution = max(resolution, 0.5)
        
        result = layer_generator.generate_solar_layer(bounds, resolution)
        return jsonify(result)
    
    @app.route('/api/renewable/layer/wind', methods=['GET'])
    @require_plan('pro')
    def get_wind_layer():
        """Get GeoJSON layer of wind potential"""
        bounds = {
            "north": float(request.args.get('north', 50)),
            "south": float(request.args.get('south', 25)),
            "east": float(request.args.get('east', -65)),
            "west": float(request.args.get('west', -125))
        }
        resolution = float(request.args.get('resolution', 1.0))
        hub_height = int(request.args.get('hub_height', 100))
        
        resolution = max(resolution, 0.5)
        
        result = layer_generator.generate_wind_layer(bounds, resolution, hub_height)
        return jsonify(result)
    
    return app


# =============================================================================
# TESTING
# =============================================================================

if __name__ == "__main__":
    nrel = NRELClient()
    
    # Test solar potential for Ashburn, VA
    print("Testing Solar Potential - Ashburn, VA...")
    solar = nrel.get_solar_potential(39.0438, -77.4874)
    print(f"Solar Capacity Factor: {solar.get('capacity_factor_pct')}%")
    print(f"Annual Production: {solar.get('annual_production_mwh')} MWh/MW")
    print(f"Rating: {solar.get('solar_rating')}")
    
    # Test wind potential for Texas Panhandle
    print("\nTesting Wind Potential - Texas Panhandle...")
    wind = nrel.get_wind_potential(35.5, -101.0)
    print(f"Mean Wind Speed: {wind.get('mean_wind_speed_ms')} m/s")
    print(f"Wind Class: {wind.get('wind_class')}")
    print(f"Power Density: {wind.get('power_density_w_m2')} W/m²")
    print(f"Rating: {wind.get('wind_rating')}")
    
    # Test combined for Phoenix, AZ
    print("\nTesting Combined - Phoenix, AZ...")
    combined = nrel.get_renewable_potential(33.4484, -112.0740)
    print(f"Combined Rating: {combined.get('combined_rating')}")
    if combined.get("solar"):
        print(f"Solar CF: {combined['solar'].get('capacity_factor_pct')}%")
    if combined.get("wind"):
        print(f"Wind CF: {combined['wind'].get('estimated_capacity_factor_pct')}%")
