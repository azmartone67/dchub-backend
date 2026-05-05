"""
DC Hub - ISO Integrations for ERCOT and PJM
Live grid data including fuel mix, pricing, and demand
"""

import os
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from functools import lru_cache
import time


# =============================================================================
# GRIDSTATUS API INTEGRATION (Recommended - Free Tier Available)
# =============================================================================

class GridStatusClient:
    """
    Client for GridStatus.io API - provides live ISO data
    Free tier: 1000 requests/month
    Sign up at: https://www.gridstatus.io/
    """
    
    BASE_URL = "https://api.gridstatus.io/v1"
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("GRIDSTATUS_API_KEY")
        self.session = requests.Session()
        if self.api_key:
            self.session.headers["X-Api-Key"] = self.api_key
    
    def _request(self, endpoint: str, params: Dict = None) -> Dict:
        """Make authenticated request to GridStatus API"""
        url = f"{self.BASE_URL}{endpoint}"
        try:
            response = self.session.get(url, params=params, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            return {"error": str(e), "status": "failed"}
    
    def get_fuel_mix(self, iso: str) -> Dict:
        """
        Get current fuel mix for an ISO
        Supported: CAISO, ERCOT, ISONE, MISO, NYISO, PJM, SPP
        """
        iso = iso.upper()
        endpoint = f"/datasets/{iso.lower()}/fuel_mix/latest"
        data = self._request(endpoint)
        
        if "error" not in data:
            return self._format_fuel_mix(data, iso)
        return data
    
    def get_load(self, iso: str) -> Dict:
        """Get current load/demand for an ISO"""
        iso = iso.upper()
        endpoint = f"/datasets/{iso.lower()}/load/latest"
        return self._request(endpoint)
    
    def get_lmp_prices(self, iso: str, zone: Optional[str] = None) -> Dict:
        """
        Get Locational Marginal Prices (LMPs)
        These are the wholesale electricity prices at specific nodes
        """
        iso = iso.upper()
        endpoint = f"/datasets/{iso.lower()}/lmp/latest"
        params = {"zone": zone} if zone else {}
        return self._request(endpoint, params)
    
    def _format_fuel_mix(self, data: Dict, iso: str) -> Dict:
        """Format fuel mix data for DC Hub frontend"""
        if not data.get("data"):
            return {"error": "No data available", "iso": iso}
        
        latest = data["data"][0] if isinstance(data["data"], list) else data["data"]
        
        # Calculate percentages and format
        fuel_sources = {}
        total_mw = 0
        
        for key, value in latest.items():
            if key not in ["interval_start_utc", "interval_end_utc", "time"]:
                if isinstance(value, (int, float)) and value > 0:
                    fuel_sources[key] = value
                    total_mw += value
        
        # Convert to percentages
        fuel_mix = []
        for source, mw in fuel_sources.items():
            fuel_mix.append({
                "source": self._clean_source_name(source),
                "mw": round(mw, 1),
                "percentage": round((mw / total_mw) * 100, 1) if total_mw > 0 else 0
            })
        
        # Sort by percentage descending
        fuel_mix.sort(key=lambda x: x["percentage"], reverse=True)
        
        return {
            "iso": iso,
            "timestamp": latest.get("interval_start_utc", datetime.utcnow().isoformat()),
            "total_generation_mw": round(total_mw, 1),
            "fuel_mix": fuel_mix,
            "source": "GridStatus.io"
        }
    
    def _clean_source_name(self, source: str) -> str:
        """Clean up fuel source names for display"""
        mapping = {
            "solar": "Solar",
            "wind": "Wind",
            "natural_gas": "Natural Gas",
            "coal": "Coal",
            "nuclear": "Nuclear",
            "hydro": "Hydro",
            "other": "Other",
            "imports": "Imports",
            "battery": "Battery Storage",
            "geothermal": "Geothermal",
            "biomass": "Biomass",
            "oil": "Oil"
        }
        return mapping.get(source.lower(), source.replace("_", " ").title())


# =============================================================================
# DIRECT ISO API INTEGRATIONS (Fallback - No API Key Required)
# =============================================================================

class ERCOTClient:
    """
    Direct ERCOT API client
    Public data available at: https://www.ercot.com/gridinfo
    """
    
    BASE_URL = "https://www.ercot.com/api/1/services/read"
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "DC-Hub/1.0 (Data Center Intelligence)"
    
    def get_fuel_mix(self) -> Dict:
        """Get current ERCOT fuel mix from public feed"""
        try:
            # ERCOT's public fuel mix endpoint
            url = "https://www.ercot.com/api/1/services/read/dashboards/fuel-mix.json"
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            data = response.json()
            
            return self._format_ercot_fuel_mix(data)
        except Exception as e:
            return {"error": str(e), "iso": "ERCOT", "status": "failed"}
    
    def get_system_conditions(self) -> Dict:
        """Get current ERCOT system conditions"""
        try:
            url = "https://www.ercot.com/api/1/services/read/dashboards/systemConditions.json"
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            return {"error": str(e)}
    
    def get_real_time_prices(self) -> Dict:
        """Get ERCOT real-time settlement point prices"""
        try:
            url = "https://www.ercot.com/api/1/services/read/dashboards/rtmSpp.json"
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            return {"error": str(e)}
    
    def _format_ercot_fuel_mix(self, data: Dict) -> Dict:
        """Format ERCOT fuel mix data"""
        if not data.get("currentGeneration"):
            return {"error": "No generation data", "iso": "ERCOT"}
        
        gen_data = data["currentGeneration"]
        fuel_mix = []
        total_mw = 0
        
        fuel_mapping = {
            "Coal": "Coal",
            "Gas": "Natural Gas", 
            "Gas-CC": "Natural Gas (Combined Cycle)",
            "Hydro": "Hydro",
            "Nuclear": "Nuclear",
            "Other": "Other",
            "Power Storage": "Battery Storage",
            "Solar": "Solar",
            "Wind": "Wind"
        }
        
        for item in gen_data:
            if isinstance(item, dict):
                source = item.get("fuel", item.get("fuelType", "Unknown"))
                mw = float(item.get("gen", item.get("MW", 0)))
                if mw > 0:
                    fuel_mix.append({
                        "source": fuel_mapping.get(source, source),
                        "mw": round(mw, 1),
                        "percentage": 0  # Calculate after total
                    })
                    total_mw += mw
        
        # Calculate percentages
        for item in fuel_mix:
            item["percentage"] = round((item["mw"] / total_mw) * 100, 1) if total_mw > 0 else 0
        
        fuel_mix.sort(key=lambda x: x["percentage"], reverse=True)
        
        return {
            "iso": "ERCOT",
            "timestamp": datetime.utcnow().isoformat(),
            "total_generation_mw": round(total_mw, 1),
            "fuel_mix": fuel_mix,
            "source": "ERCOT Public API"
        }


class PJMClient:
    """
    PJM API client
    Public data: https://dataminer2.pjm.com/
    """
    
    BASE_URL = "https://api.pjm.com/api/v1"
    DATAMINER_URL = "https://dataminer2.pjm.com/feed"
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("PJM_API_KEY")
        self.session = requests.Session()
        if self.api_key:
            self.session.headers["Ocp-Apim-Subscription-Key"] = self.api_key
    
    def get_fuel_mix(self) -> Dict:
        """Get current PJM fuel mix"""
        try:
            # PJM Data Miner feed for instantaneous fuel mix
            url = f"{self.DATAMINER_URL}/inst_gen_by_fuel/json"
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            data = response.json()
            
            return self._format_pjm_fuel_mix(data)
        except Exception as e:
            return {"error": str(e), "iso": "PJM", "status": "failed"}
    
    def get_load(self) -> Dict:
        """Get current PJM load"""
        try:
            url = f"{self.DATAMINER_URL}/inst_load/json"
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            return {"error": str(e)}
    
    def get_lmp_prices(self, zone: str = "WEST") -> Dict:
        """Get PJM Locational Marginal Prices"""
        try:
            url = f"{self.DATAMINER_URL}/rt_fivemin_lmps/json"
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            return {"error": str(e)}
    
    def _format_pjm_fuel_mix(self, data: List) -> Dict:
        """Format PJM fuel mix data"""
        if not data:
            return {"error": "No data available", "iso": "PJM"}
        
        fuel_mix = []
        total_mw = 0
        
        fuel_mapping = {
            "Coal": "Coal",
            "Gas": "Natural Gas",
            "Hydro": "Hydro",
            "Multiple Fuels": "Mixed",
            "Nuclear": "Nuclear",
            "Oil": "Oil",
            "Other": "Other",
            "Other Renewables": "Other Renewables",
            "Solar": "Solar",
            "Storage": "Battery Storage",
            "Wind": "Wind"
        }
        
        # Get latest data point
        latest = data[-1] if isinstance(data, list) else data
        
        for key, value in latest.items():
            if key not in ["datetime_beginning_utc", "datetime_beginning_ept", "is_verified"]:
                if isinstance(value, (int, float)) and value > 0:
                    source_name = fuel_mapping.get(key, key)
                    fuel_mix.append({
                        "source": source_name,
                        "mw": round(value, 1),
                        "percentage": 0
                    })
                    total_mw += value
        
        # Calculate percentages
        for item in fuel_mix:
            item["percentage"] = round((item["mw"] / total_mw) * 100, 1) if total_mw > 0 else 0
        
        fuel_mix.sort(key=lambda x: x["percentage"], reverse=True)
        
        return {
            "iso": "PJM",
            "timestamp": latest.get("datetime_beginning_utc", datetime.utcnow().isoformat()),
            "total_generation_mw": round(total_mw, 1),
            "fuel_mix": fuel_mix,
            "source": "PJM DataMiner"
        }


# =============================================================================
# UNIFIED ISO SERVICE
# =============================================================================

class ISOService:
    """
    Unified service for all ISO data
    Handles failover between GridStatus API and direct ISO APIs
    """
    
    SUPPORTED_ISOS = ["CAISO", "ERCOT", "ISONE", "MISO", "NYISO", "PJM", "SPP"]
    
    def __init__(self, gridstatus_key: Optional[str] = None, pjm_key: Optional[str] = None):
        self.gridstatus = GridStatusClient(gridstatus_key)
        self.ercot = ERCOTClient()
        self.pjm = PJMClient(pjm_key)
        self._cache = {}
        self._cache_ttl = 300  # 5 minute cache
    
    EIA_FUEL_MIX_FALLBACK = {
        'ERCOT': {'Natural Gas': 42.3, 'Wind': 25.1, 'Coal': 14.2, 'Nuclear': 10.8, 'Solar': 5.9, 'Other': 1.7},
        'PJM': {'Natural Gas': 38.5, 'Nuclear': 32.1, 'Coal': 15.8, 'Wind': 5.2, 'Solar': 2.1, 'Hydro': 1.8, 'Other': 4.5},
        'CAISO': {'Natural Gas': 37.8, 'Solar': 22.4, 'Wind': 10.2, 'Hydro': 11.5, 'Nuclear': 8.9, 'Imports': 6.1, 'Other': 3.1},
        'NYISO': {'Natural Gas': 36.2, 'Nuclear': 25.8, 'Hydro': 22.1, 'Wind': 5.3, 'Solar': 2.4, 'Other': 8.2},
        'MISO': {'Natural Gas': 32.1, 'Coal': 25.3, 'Wind': 18.9, 'Nuclear': 14.2, 'Solar': 3.8, 'Hydro': 2.1, 'Other': 3.6},
        'SPP': {'Natural Gas': 28.5, 'Wind': 38.2, 'Coal': 22.1, 'Solar': 4.8, 'Hydro': 2.3, 'Nuclear': 1.2, 'Other': 2.9},
        'ISONE': {'Natural Gas': 52.1, 'Nuclear': 22.3, 'Hydro': 7.8, 'Wind': 5.2, 'Solar': 6.1, 'Other': 6.5}
    }

    def get_fuel_mix(self, iso: str) -> Dict:
        """Get fuel mix for any supported ISO with fallback"""
        iso = iso.upper()
        
        if iso not in self.SUPPORTED_ISOS:
            return {
                "error": f"Unsupported ISO: {iso}",
                "supported": self.SUPPORTED_ISOS
            }
        
        cache_key = f"fuel_mix_{iso}"
        if cache_key in self._cache:
            cached, timestamp = self._cache[cache_key]
            if time.time() - timestamp < self._cache_ttl:
                return cached
        
        result = self.gridstatus.get_fuel_mix(iso)
        
        if "error" in result:
            if iso == "ERCOT":
                result = self.ercot.get_fuel_mix()
            elif iso == "PJM":
                result = self.pjm.get_fuel_mix()
        
        if "error" in result and iso in self.EIA_FUEL_MIX_FALLBACK:
            mix = self.EIA_FUEL_MIX_FALLBACK[iso]
            fuel_mix = [{"source": k, "percentage": v, "mw": None} for k, v in mix.items()]
            fuel_mix.sort(key=lambda x: x["percentage"], reverse=True)
            result = {
                "iso": iso,
                "timestamp": datetime.utcnow().isoformat(),
                "fuel_mix": fuel_mix,
                "source": "EIA Annual Average (2024)",
                "note": "Live data temporarily unavailable, showing EIA annual averages"
            }
        
        if "error" not in result:
            self._cache[cache_key] = (result, time.time())
        
        return result
    
    def get_all_isos(self) -> Dict:
        """Get fuel mix data for all supported ISOs"""
        results = {}
        for iso in self.SUPPORTED_ISOS:
            results[iso] = self.get_fuel_mix(iso)
        return results
    
    def get_grid_summary(self, iso: str) -> Dict:
        """Get comprehensive grid summary including prices and load"""
        iso = iso.upper()
        
        summary = {
            "iso": iso,
            "timestamp": datetime.utcnow().isoformat(),
            "fuel_mix": self.get_fuel_mix(iso)
        }
        
        # Add ISO-specific data
        if iso == "ERCOT":
            summary["system_conditions"] = self.ercot.get_system_conditions()
            summary["prices"] = self.ercot.get_real_time_prices()
        elif iso == "PJM":
            summary["load"] = self.pjm.get_load()
            summary["prices"] = self.pjm.get_lmp_prices()
        
        return summary


# =============================================================================
# FLASK ROUTES (Add these to your main.py)
# =============================================================================

def register_iso_routes(app):
    """Register ISO API routes with Flask app"""
    from flask import jsonify, request
    from api_tier_gating import require_plan
    
    iso_service = ISOService()
    
    @app.route('/api/grid/fuel-mix', methods=['GET'])
    @require_plan('pro')
    def get_fuel_mix():
        """
        Get fuel mix for an ISO
        Query params:
            - iso: ISO name (CAISO, ERCOT, ISONE, MISO, NYISO, PJM, SPP)
        """
        iso = request.args.get('iso', 'ERCOT').upper()
        result = iso_service.get_fuel_mix(iso)
        return jsonify(result)
    
    @app.route('/api/grid/all-isos', methods=['GET'])
    @require_plan('pro')
    def get_all_isos():
        """Get fuel mix for all ISOs"""
        result = iso_service.get_all_isos()
        return jsonify(result)
    
    @app.route('/api/grid/summary/<iso>', methods=['GET'])
    @require_plan('pro')
    def get_grid_summary(iso):
        """Get comprehensive grid summary for an ISO"""
        result = iso_service.get_grid_summary(iso)
        return jsonify(result)
    
    @app.route('/api/grid/supported-isos', methods=['GET'])
    @require_plan('pro')
    def get_supported_isos():
        """Get list of supported ISOs"""
        return jsonify({
            "supported_isos": ISOService.SUPPORTED_ISOS,
            "coverage": {
                "CAISO": "California",
                "ERCOT": "Texas",
                "ISONE": "New England",
                "MISO": "Midwest",
                "NYISO": "New York",
                "PJM": "Mid-Atlantic & Midwest (13 states + DC)",
                "SPP": "Southwest Power Pool"
            }
        })
    
    return app


# =============================================================================
# TESTING
# =============================================================================

if __name__ == "__main__":
    # Test the ISO service
    service = ISOService()
    
    print("Testing ERCOT fuel mix...")
    result = service.get_fuel_mix("ERCOT")
    print(f"ERCOT: {result}")
    
    print("\nTesting PJM fuel mix...")
    result = service.get_fuel_mix("PJM")
    print(f"PJM: {result}")
