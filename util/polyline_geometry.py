"""True length and home state of a WGS84 polyline — for transmission_lines.

★ WHY. Every transmission_lines row carried state NULL and length_miles 0
(94,619 of 94,619, measured in prod 2026-09-23), so land_power_crawler's
market profile published transmission_line_count = 0 and
total_transmission_miles = 0 for every state. The weekly EIA ingest fetched
attributes only and never set either column.

The EIA layer does carry Shape__Length, but it is measured in the service's
Web Mercator (EPSG:3857) metres, which are inflated by sec(latitude): +31% at
40°N, +56% at 50°N, +180% at 69°N (the North Slope). Summed per state that is
not a length, it is a length times a latitude-dependent error. So the runner
asks for the geometry in EPSG:4326 and measures it here instead.

LENGTH — each segment on the WGS84 ellipsoid, using the meridional (M) and
prime-vertical (N) radii of curvature at the segment's mid-latitude. The
error of that local approximation grows with the square of segment length
(~6e-5 relative for a 50 km segment); EIA lines average ~30 vertices, so
segments are short.

STATE — the state containing the point HALFWAY ALONG the line (by length), via
util/state_polygons (Census cartographic boundaries, offline, committed). A
line that crosses a border is attributed whole to one state; that is the cost
of a one-row-one-state table. When the halfway point is in no state (a
submarine cable, a bay crossing, a tie line whose middle is in Canada), the
vertex nearest the halfway point BY DISTANCE ALONG THE LINE that is inside a
state wins. A line with no vertex in any US area gets '' — an answer, not a
gap. If the boundary dataset itself fails to load, StateBoundariesUnavailable
is raised: silently writing NULL for every row is the defect this replaces.
"""
import math

_A = 6378137.0                     # WGS84 semi-major axis, metres
_F = 1 / 298.257223563
_E2 = _F * (2 - _F)
METERS_PER_MILE = 1609.344


class StateBoundariesUnavailable(RuntimeError):
    """util/state_polygons could not load its dataset — no state can be named."""


def _segment_m(lng1, lat1, lng2, lat2):
    phi = math.radians((lat1 + lat2) / 2.0)
    s = math.sin(phi)
    w = 1.0 - _E2 * s * s
    m = _A * (1.0 - _E2) / (w ** 1.5)          # meridional radius
    n = _A / math.sqrt(w)                       # prime-vertical radius
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(((lng2 - lng1 + 180.0) % 360.0) - 180.0)
    return math.hypot(m * dphi, n * math.cos(phi) * dlam)


def _clean_paths(paths):
    """[[ [x, y], ... ], ...] -> list of vertex lists, bad vertices dropped."""
    out = []
    for path in paths or []:
        verts = []
        for pt in path or []:
            try:
                x, y = float(pt[0]), float(pt[1])
            except (TypeError, ValueError, IndexError):
                continue
            if math.isfinite(x) and math.isfinite(y) and -180 <= x <= 180 and -90 <= y <= 90:
                verts.append((x, y))
        if verts:
            out.append(verts)
    return out


def _walk(paths):
    """Vertices as (lng, lat, metres-along-line), paths chained without the gap."""
    walked, total = [], 0.0
    for verts in paths:
        prev = None
        for x, y in verts:
            if prev is not None:
                total += _segment_m(prev[0], prev[1], x, y)
            walked.append((x, y, total))
            prev = (x, y)
    return walked, total


def _point_at(walked, target):
    for i in range(1, len(walked)):
        x0, y0, d0 = walked[i - 1]
        x1, y1, d1 = walked[i]
        if d1 >= target:
            t = 0.0 if d1 == d0 else (target - d0) / (d1 - d0)
            return x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
    return walked[-1][0], walked[-1][1]


def _state_at(lng, lat):
    from util.state_polygons import load_error, state_containing
    code = state_containing(lat, lng)
    if code is None:
        raise StateBoundariesUnavailable(load_error() or "state boundaries did not load")
    return code


def measure_line(paths):
    """(length_miles, state) for an Esri polyline's `paths` in EPSG:4326.

    length_miles: float rounded to 3 dp, or None when there is no usable vertex.
    state: two-letter code, '' when no vertex is in a US area, None when there
    is no usable vertex.
    """
    cleaned = _clean_paths(paths)
    if not cleaned:
        return None, None
    walked, total = _walk(cleaned)
    half = total / 2.0
    lng, lat = _point_at(walked, half)
    state = _state_at(lng, lat)
    if not state:
        for x, y, _d in sorted(walked, key=lambda v: abs(v[2] - half)):
            state = _state_at(x, y)
            if state:
                break
    return round(total / METERS_PER_MILE, 3), state
