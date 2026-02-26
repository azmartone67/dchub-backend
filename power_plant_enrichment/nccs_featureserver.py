"""
NASA NCCS FeatureServer - HIFLD Power Plant Mirror
===================================================
Queries the NASA NCCS-hosted mirror of HIFLD open energy data
for power plant locations and attributes.

Endpoint: maps.nccs.nasa.gov/mapping/rest/services/hifld_open/energy/FeatureServer
Layers:   0 = Power Plants, 1 = Transmission Lines, 2 = Substations, etc.

This is a standard ArcGIS REST API - supports pagination, spatial queries,
and field filtering.
"""

import logging
import time
from datetime import datetime
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Base endpoint
NCCS_BASE = "https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/energy/FeatureServer"

# Known layer IDs (verify with /FeatureServer?f=json)
LAYER_POWER_PLANTS = 0
LAYER_TRANSMISSION_LINES = 1
LAYER_SUBSTATIONS = 2

# ArcGIS REST API pagination limit
MAX_RECORD_COUNT = 2000

# Field mappings from HIFLD schema to our standard format
HIFLD_FIELD_MAP = {
    "OBJECTID": "hifld_object_id",
    "NAME": "name",
    "STATE": "state",
    "COUNTY": "county",
    "LATITUDE": "latitude",
    "LONGITUDE": "longitude",
    "NAICS_CODE": "naics_code",
    "SOURCE": "hifld_source",
    "SOURCEDATE": "source_date",
    "STATUS": "status",
    "LINES": "transmission_lines",
    "MAX_MW": "capacity_mw",
    "PRIM_FUEL": "energy_source",
    "TOTAL_MW": "total_capacity_mw",
    "PLNT_PRMR": "prime_mover",
}


class NCCSFeatureServerClient:
    """Client for querying NASA NCCS ArcGIS FeatureServer for HIFLD data."""

    def __init__(self, base_url: str = NCCS_BASE, timeout: int = 30,
                 max_retries: int = 3, retry_delay: float = 2.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "DCHub-DataEnrichment/1.0 (dchub.cloud)"
        })

    def get_service_info(self) -> dict:
        """Fetch FeatureServer metadata (layers, capabilities, etc.)."""
        resp = self._request(f"{self.base_url}", params={"f": "json"})
        return resp

    def get_layer_info(self, layer_id: int = LAYER_POWER_PLANTS) -> dict:
        """Fetch layer metadata (fields, record count, extent)."""
        resp = self._request(
            f"{self.base_url}/{layer_id}",
            params={"f": "json"}
        )
        return resp

    def get_record_count(self, layer_id: int = LAYER_POWER_PLANTS,
                         where: str = "1=1") -> int:
        """Get total record count for a layer query."""
        resp = self._request(
            f"{self.base_url}/{layer_id}/query",
            params={
                "where": where,
                "returnCountOnly": "true",
                "f": "json"
            }
        )
        return resp.get("count", 0)

    def query_power_plants(
        self,
        where: str = "1=1",
        out_fields: str = "*",
        state_filter: Optional[str] = None,
        min_capacity_mw: Optional[float] = None,
        fuel_type: Optional[str] = None,
        bbox: Optional[tuple[float, float, float, float]] = None,
        return_geometry: bool = True,
        max_records: Optional[int] = None,
    ) -> list[dict]:
        """
        Query power plants with optional filters. Auto-paginates.

        Args:
            where: SQL WHERE clause (default: all records).
            out_fields: Comma-separated fields or "*" for all.
            state_filter: Two-letter state code (e.g., "VA").
            min_capacity_mw: Minimum capacity filter.
            fuel_type: Primary fuel type filter (e.g., "NG", "SUN", "WND").
            bbox: Bounding box as (xmin, ymin, xmax, ymax) in WGS84.
            return_geometry: Include geometry in response.
            max_records: Limit total records returned.

        Returns:
            List of plant dicts with standardized field names.
        """
        # Build WHERE clause from filters
        conditions = []
        if where != "1=1":
            conditions.append(where)
        if state_filter:
            conditions.append(f"STATE = '{state_filter.upper()}'")
        if min_capacity_mw is not None:
            conditions.append(f"TOTAL_MW >= {min_capacity_mw}")
        if fuel_type:
            conditions.append(f"PRIM_FUEL = '{fuel_type.upper()}'")

        final_where = " AND ".join(conditions) if conditions else "1=1"

        # Get total count first
        total = self.get_record_count(LAYER_POWER_PLANTS, final_where)
        logger.info(f"NCCS query matches {total} power plants (where: {final_where})")

        if max_records:
            total = min(total, max_records)

        # Paginate through results
        all_features = []
        offset = 0

        while offset < total:
            params = {
                "where": final_where,
                "outFields": out_fields,
                "returnGeometry": str(return_geometry).lower(),
                "resultOffset": offset,
                "resultRecordCount": min(MAX_RECORD_COUNT, total - offset),
                "f": "geojson",  # GeoJSON for easy lat/lng extraction
                "outSR": "4326",  # WGS84
            }

            if bbox:
                params["geometry"] = f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}"
                params["geometryType"] = "esriGeometryEnvelope"
                params["inSR"] = "4326"
                params["spatialRel"] = "esriSpatialRelIntersects"

            resp = self._request(
                f"{self.base_url}/{LAYER_POWER_PLANTS}/query",
                params=params
            )

            features = resp.get("features", [])
            if not features:
                break

            all_features.extend(features)
            offset += len(features)

            logger.info(f"  Fetched {len(all_features)}/{total} plants...")

            # Respect rate limits
            if offset < total:
                time.sleep(0.5)

        # Normalize to standard format
        plants = [self._normalize_feature(f) for f in all_features]
        plants = [p for p in plants if p is not None]

        logger.info(f"Retrieved {len(plants)} plants from NCCS FeatureServer")
        return plants

    def query_by_proximity(
        self,
        lat: float,
        lng: float,
        radius_miles: float = 50,
        **kwargs
    ) -> list[dict]:
        """
        Find power plants within a radius of a point.

        Args:
            lat: Center latitude.
            lng: Center longitude.
            radius_miles: Search radius in miles.
            **kwargs: Additional filters passed to query_power_plants.

        Returns:
            List of plant dicts sorted by distance.
        """
        # Convert miles to approximate degrees for bbox
        # 1 degree lat ≈ 69 miles, 1 degree lng varies by latitude
        lat_offset = radius_miles / 69.0
        lng_offset = radius_miles / (69.0 * abs(cos_deg(lat)))

        bbox = (
            lng - lng_offset,  # xmin
            lat - lat_offset,  # ymin
            lng + lng_offset,  # xmax
            lat + lat_offset,  # ymax
        )

        plants = self.query_power_plants(bbox=bbox, **kwargs)

        # Calculate actual distances and sort
        for plant in plants:
            if plant.get("latitude") and plant.get("longitude"):
                plant["distance_miles"] = haversine_miles(
                    lat, lng, plant["latitude"], plant["longitude"]
                )

        plants.sort(key=lambda p: p.get("distance_miles", float("inf")))

        # Filter to actual radius (bbox is rectangular)
        plants = [p for p in plants if p.get("distance_miles", float("inf")) <= radius_miles]

        return plants

    def _normalize_feature(self, feature: dict) -> Optional[dict]:
        """Convert a GeoJSON feature to our standard plant format."""
        props = feature.get("properties", {})
        geom = feature.get("geometry", {})

        # Extract coordinates from GeoJSON point
        lat, lng = None, None
        if geom and geom.get("type") == "Point":
            coords = geom.get("coordinates", [])
            if len(coords) >= 2:
                lng, lat = coords[0], coords[1]

        # Fall back to attribute fields
        if lat is None:
            lat = _safe_float(props.get("LATITUDE"))
        if lng is None:
            lng = _safe_float(props.get("LONGITUDE"))

        if lat is None or lng is None:
            return None

        # Map fields to standard names
        plant = {
            "latitude": lat,
            "longitude": lng,
            "source": "nccs_hifld",
            "updated_at": datetime.utcnow().isoformat(),
        }

        for hifld_field, our_field in HIFLD_FIELD_MAP.items():
            val = props.get(hifld_field)
            if val is not None:
                if our_field in ("capacity_mw", "total_capacity_mw"):
                    plant[our_field] = _safe_float(val)
                else:
                    plant[our_field] = str(val).strip() if val else None

        return plant

    def _request(self, url: str, params: dict) -> dict:
        """Make an HTTP request with retry logic."""
        last_error = None
        for attempt in range(self.max_retries):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                resp.raise_for_status()
                data = resp.json()

                # Check for ArcGIS error responses
                if "error" in data:
                    error = data["error"]
                    raise RuntimeError(
                        f"ArcGIS error {error.get('code')}: {error.get('message')}"
                    )

                return data

            except (requests.RequestException, RuntimeError) as e:
                last_error = e
                logger.warning(
                    f"Request failed (attempt {attempt + 1}/{self.max_retries}): {e}"
                )
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay * (attempt + 1))

        raise last_error


# ─── Utility functions ───────────────────────────────────────────────

import math

def cos_deg(degrees: float) -> float:
    return math.cos(math.radians(degrees))

def haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Calculate distance between two points in miles."""
    R = 3959  # Earth radius in miles
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlng / 2) ** 2)
    c = 2 * math.asin(math.sqrt(a))
    return R * c

def _safe_float(val) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None
