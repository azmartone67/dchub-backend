"""Phase 23 — site-eval integration: which ISO covers a lat/lon, with live data."""
import requests

# Rough lat/lon bounding boxes per ISO (good enough for first-pass selection)
ISO_BOXES = {
    'ERCOT': (25.5, -107.0, 36.6, -93.5),
    'CAISO': (32.5, -125.0, 42.1, -114.0),
    'NYISO': (40.4, -79.8, 45.0, -71.8),
    'ISONE': (41.0, -73.7, 47.5, -66.9),
    'SPP':   (29.5, -106.7, 49.0, -94.4),
    'MISO':  (29.5, -104.0, 49.5, -82.5),
    'PJM':   (35.0, -90.5, 43.0, -73.5),
}


def iso_for_latlon(lat, lon):
    """Return list of ISO codes whose bbox contains the point."""
    if lat is None or lon is None: return []
    out = []
    for iso, (s, w, n, e) in ISO_BOXES.items():
        if s <= lat <= n and w <= lon <= e:
            out.append(iso)
    # Pick most specific (smallest bbox area) first
    out.sort(key=lambda i: (ISO_BOXES[i][2]-ISO_BOXES[i][0]) * (ISO_BOXES[i][3]-ISO_BOXES[i][1]))
    return out


def grid_health_for_site(lat, lon, base_url='http://127.0.0.1:8080'):
    """Return a {iso, demand_mw, headroom_pct, status} dict for the site."""
    isos = iso_for_latlon(lat, lon)
    if not isos:
        return {'iso': None, 'note': 'No US ISO covers this location'}
    iso = isos[0]
    try:
        r = requests.get(f'{base_url}/api/v1/grid/intelligence/{iso}', timeout=6)
        d = r.json().get('data', {}) if r.ok else {}
        demand = d.get('current_demand_mw', 0) or 0
        cap = d.get('total_capacity_mw', 0) or 0
        headroom = d.get('headroom_pct', 0) or 0
        if headroom > 30:    status = 'healthy'
        elif headroom > 15:  status = 'tight'
        else:                status = 'constrained'
        return {
            'iso': iso,
            'iso_name': iso,
            'demand_mw': int(demand),
            'capacity_mw': int(cap),
            'headroom_pct': round(headroom, 1),
            'status': status,
            'covered_by': isos,
        }
    except Exception as _e:
        return {'iso': iso, 'error': str(_e)[:120]}
