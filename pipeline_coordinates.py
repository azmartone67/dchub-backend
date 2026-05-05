"""
DC Hub - Pipeline Coordinate Enhancement
Adds geographic coordinates to pipeline records based on county/market names

Add this to your backend and call enhance_pipeline_coordinates() when returning pipeline data
"""

# Pennsylvania county centroids (lat, lon)
PA_COUNTY_COORDS = {
    "McKean County": (41.8086, -78.5636),
    "Potter County": (41.7448, -77.8975),
    "Warren County": (41.8145, -79.2753),
    "Erie County": (42.1167, -80.0733),
    "Tioga County": (41.7728, -77.2539),
    "Bradford County": (41.7889, -76.5147),
    "Lycoming County": (41.3431, -77.0017),
    "Clinton County": (41.2345, -77.6389),
    "Cameron County": (41.4356, -78.2003),
    "Elk County": (41.4242, -78.6492),
    "Clearfield County": (41.0006, -78.4739),
    "Centre County": (40.9192, -77.8197),
    "Jefferson County": (41.1281, -78.9997),
    "Indiana County": (40.6531, -79.0867),
    "Cambria County": (40.4942, -78.7139),
    "Somerset County": (39.9722, -79.0281),
    "Fayette County": (39.9136, -79.6489),
    "Westmoreland County": (40.3097, -79.4672),
    "Allegheny County": (40.4689, -79.9819),
    "Washington County": (40.1894, -80.2489),
    "Greene County": (39.8522, -80.2247),
    "Beaver County": (40.6822, -80.3511),
    "Butler County": (40.9117, -79.9156),
    "Armstrong County": (40.8122, -79.4656),
    "Venango County": (41.4036, -79.7581),
    "Mercer County": (41.3028, -80.2581),
    "Crawford County": (41.6847, -80.1067),
    "Lawrence County": (40.9908, -80.3344),
    "Pennsylvania": (40.8781, -77.7996),  # State centroid fallback
}

# Texas county centroids
TX_COUNTY_COORDS = {
    "Permian Basin": (31.8457, -102.3676),
    "Webb County": (27.7606, -99.3312),
    "Midland County": (31.8693, -102.0311),
    "Ector County": (31.8493, -102.5176),
    "Reeves County": (31.3243, -103.6932),
    "Pecos County": (30.7849, -102.7132),
    "Ward County": (31.5076, -103.1007),
    "Winkler County": (31.8493, -103.0507),
    "Loving County": (31.8493, -103.5007),
    "Culberson County": (31.4493, -104.5007),
    "Harris County": (29.7752, -95.3103),
    "Tarrant County": (32.7767, -97.3097),
    "Dallas County": (32.7767, -96.7970),
    "Texas": (31.9686, -99.9018),  # State centroid fallback
}

# Louisiana parish centroids
LA_PARISH_COORDS = {
    "Calcasieu Parish": (30.2266, -93.3544),
    "Cameron Parish": (29.8016, -93.3294),
    "Jefferson Parish": (29.6727, -90.1053),
    "Plaquemines Parish": (29.4427, -89.6803),
    "St. Charles Parish": (29.9527, -90.3553),
    "Louisiana": (30.9843, -91.9623),  # State centroid fallback
}

# Combined lookup
COUNTY_COORDS = {
    **PA_COUNTY_COORDS,
    **TX_COUNTY_COORDS,
    **LA_PARISH_COORDS,
}


def get_county_coordinates(market: str, state: str = None) -> tuple:
    """
    Get coordinates for a county/market name
    Returns (lat, lon) tuple or None if not found
    """
    if not market:
        return None
    
    # Try exact match first
    if market in COUNTY_COORDS:
        return COUNTY_COORDS[market]
    
    # Try with "County" suffix
    market_with_county = f"{market} County"
    if market_with_county in COUNTY_COORDS:
        return COUNTY_COORDS[market_with_county]
    
    # Try state fallback
    if state and state in COUNTY_COORDS:
        return COUNTY_COORDS[state]
    
    return None


def generate_pipeline_route(start_coords: tuple, end_coords: tuple = None, num_points: int = 5) -> list:
    """
    Generate a simple pipeline route between two points
    If no end_coords, generates a short line segment from start
    Returns list of [lon, lat] coordinates for GeoJSON
    """
    if not start_coords:
        return None
    
    lat1, lon1 = start_coords
    
    if end_coords:
        lat2, lon2 = end_coords
    else:
        # Generate a short segment (roughly 10-20 miles in a random direction)
        import random
        offset = random.uniform(0.1, 0.2)  # ~7-14 miles
        direction = random.choice([(1, 0), (0, 1), (1, 1), (-1, 1)])
        lat2 = lat1 + (offset * direction[0])
        lon2 = lon1 + (offset * direction[1])
    
    # Generate intermediate points for a smoother line
    coords = []
    for i in range(num_points):
        t = i / (num_points - 1)
        lat = lat1 + (lat2 - lat1) * t
        lon = lon1 + (lon2 - lon1) * t
        # Add small random offset for natural look
        import random
        lat += random.uniform(-0.01, 0.01)
        lon += random.uniform(-0.01, 0.01)
        coords.append([round(lon, 6), round(lat, 6)])  # GeoJSON is [lon, lat]
    
    return coords


def enhance_pipeline_with_coordinates(pipeline: dict) -> dict:
    """
    Add coordinates to a single pipeline record
    """
    market = pipeline.get('market')
    state = pipeline.get('state')
    
    coords = get_county_coordinates(market, state)
    
    if coords:
        lat, lon = coords
        # Add point coordinates
        pipeline['lat'] = lat
        pipeline['lon'] = lon
        
        # Generate a route line
        pipeline['coordinates'] = generate_pipeline_route(coords)
        pipeline['geometry'] = {
            'type': 'LineString',
            'coordinates': pipeline['coordinates']
        }
    
    return pipeline


def enhance_pipeline_coordinates(pipelines: list) -> list:
    """
    Add coordinates to a list of pipeline records
    Call this in your API endpoint before returning data
    
    Example usage in main.py:
        
        @app.route('/api/v1/gas-pipelines')
        def get_gas_pipelines():
            pipelines = fetch_pipelines_from_db()
            enhanced = enhance_pipeline_coordinates(pipelines)
            return jsonify({'pipelines': enhanced, 'success': True})
    """
    return [enhance_pipeline_with_coordinates(p.copy()) for p in pipelines]


# ============================================================================
# INTEGRATION WITH YOUR EXISTING ENDPOINT
# ============================================================================

def integrate_with_gas_pipelines_endpoint():
    """
    Example of how to integrate with your existing /api/v1/gas-pipelines endpoint
    
    In your main.py, modify the endpoint like this:
    
    ```python
    from pipeline_coordinates import enhance_pipeline_coordinates
    
    @app.route('/api/v1/gas-pipelines')
    def get_gas_pipelines():
        state = request.args.get('state')
        operator = request.args.get('operator')
        pipeline_type = request.args.get('type')
        limit = request.args.get('limit', 100, type=int)
        
        # Your existing query logic
        pipelines = query_pipelines(state, operator, pipeline_type, limit)
        
        # ADD THIS LINE to enhance with coordinates
        pipelines = enhance_pipeline_coordinates(pipelines)
        
        return jsonify({
            'success': True,
            'pipelines': pipelines,
            'count': len(pipelines)
        })
    ```
    """
    pass


# ============================================================================
# TESTING
# ============================================================================

if __name__ == "__main__":
    # Test data matching your API response
    test_pipelines = [
        {
            "id": "nfg-pa-006",
            "operator": "National Fuel Gas Supply",
            "pipeline_type": "Transmission",
            "diameter_inches": 30.0,
            "market": "McKean County",
            "state": "PA",
            "source": "FERC",
            "status": "Active"
        },
        {
            "id": "nfg-pa-007",
            "operator": "National Fuel Gas Supply",
            "pipeline_type": "Transmission",
            "diameter_inches": 26.0,
            "market": "Potter County",
            "state": "PA",
            "source": "FERC",
            "status": "Active"
        },
        {
            "id": "nfg-pa-010",
            "operator": "National Fuel Gas Supply",
            "pipeline_type": "Gathering",
            "diameter_inches": 10.0,
            "market": "Tioga County",
            "state": "PA",
            "source": "FERC",
            "status": "Active"
        }
    ]
    
    # Enhance with coordinates
    enhanced = enhance_pipeline_coordinates(test_pipelines)
    
    # Print results
    import json
    for p in enhanced:
        print(f"\n{p['operator']} - {p['market']}")
        print(f"  Coordinates: {p.get('lat')}, {p.get('lon')}")
        print(f"  Geometry: {p.get('geometry', {}).get('type')}")
        if p.get('coordinates'):
            print(f"  Route points: {len(p['coordinates'])}")
