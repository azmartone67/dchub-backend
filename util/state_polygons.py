"""Offline point-in-polygon against the Census state boundaries.

★ WHY OFFLINE. util/state_geometry.py resolved a coordinate by asking
geo.fcc.gov per call, with a bounding-box fallback for when that service did
not answer. The primary path is correct; the fallback cannot be, and measured
on 2026-08-29 it returned:

    Toronto, Ontario (43.6532, -79.3832)  ->  'NY'   basis 'bbox_unique'
    Ashburn, Virginia (39.04, -77.49)     ->  'MD'   basis 'bbox_ambiguous'

state_geometry's own docstring names the first one — "NEW YORK's bbox contains
TORONTO" — as the reason a rectangle is not the fix. The fallback produced it
anyway, and under the MORE confident of the two bbox bases, because only one
box matched. Ashburn is the largest data-centre market on earth. Neither is a
lie (BBOX_BASIS_NOTE is attached and the alternatives are listed) but both are
wrong, and they fire exactly when an operator cannot tell that they did.

This module removes the reason to ever reach that fallback: the same Census
geometry, committed to the repo, so the answer needs no network at all.

    data/geo/us_state_boundaries.json.gz   662 KB, 56 areas
    built by scripts/build_us_state_boundaries.py from cb_2023_us_state_500k

Coverage is the 50 states, DC and the five inhabited territories. Offshore
points, the Great Lakes, and everywhere outside the US resolve to '' — the
Census cartographic files clip states at the shoreline, so that is the correct
answer rather than a gap.

Measured on this base: ~0.3 ms per warm query, ~120 ms one-time load.
"""
import gzip
import json
import os
import threading

_DATA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "geo", "us_state_boundaries.json.gz")

# Census codes that are not one of the 50 states or DC. Carried in the geometry
# (a point in San Juan must not fall through to Florida) but named here, since
# state-keyed federal APIs — the USDM call this feeds above all — take the 51.
TERRITORY_CODES = frozenset({"PR", "VI", "GU", "AS", "MP"})

_lock = threading.Lock()
_index = None          # [(code, state_bbox, [(ring_bbox, xs, ys), ...]), ...]
_meta = {}
_load_error = None


def _decode_ring(flat, scale):
    """Undo the delta encoding into PARALLEL x/y lists.

    Parallel lists rather than a list of pairs: the ray cast indexes them in a
    tight loop and this is the hot path of the module.
    """
    xs, ys, x, y = [], [], 0, 0
    for i in range(0, len(flat), 2):
        x += flat[i]
        y += flat[i + 1]
        xs.append(x / scale)
        ys.append(y / scale)
    return xs, ys


def _build_index():
    global _index, _meta, _load_error
    try:
        with gzip.open(_DATA_PATH, "rb") as fh:
            payload = json.loads(fh.read().decode("utf-8"))
    except Exception as exc:                       # missing or corrupt artifact
        _load_error = "%s: %s" % (type(exc).__name__, exc)
        _index = []
        return
    scale = payload.get("scale") or 10000
    _meta = {k: payload.get(k, "") for k in
             ("source", "source_url", "vintage", "resolution")}
    index = []
    for code, area in sorted((payload.get("areas") or {}).items()):
        rings, min_x, min_y, max_x, max_y = [], 1e9, 1e9, -1e9, -1e9
        for flat in area.get("rings") or []:
            if len(flat) < 6:                      # fewer than three vertices
                continue
            xs, ys = _decode_ring(flat, scale)
            bbox = (min(xs), min(ys), max(xs), max(ys))
            rings.append((bbox, xs, ys))
            min_x, min_y = min(min_x, bbox[0]), min(min_y, bbox[1])
            max_x, max_y = max(max_x, bbox[2]), max(max_y, bbox[3])
        if rings:
            index.append((code, (min_x, min_y, max_x, max_y), rings))
    _index = index


def _get_index():
    global _index
    if _index is None:
        with _lock:
            if _index is None:
                _build_index()
    return _index


def _ring_contains(xs, ys, px, py):
    """Crossing-number test for one ring.

    Counted across every ring of an area (even-odd), so an interior hole
    subtracts itself and ring orientation never has to be trusted.
    """
    inside, n = False, len(xs)
    j = n - 1
    for i in range(n):
        yi, yj = ys[i], ys[j]
        if (yi > py) != (yj > py):
            # x of the edge at py; the (yi > py) != (yj > py) guard above is
            # what makes the division safe.
            if px < xs[i] + (py - yi) * (xs[j] - xs[i]) / (yj - yi):
                inside = not inside
        j = i
    return inside


def state_containing(lat, lng):
    """Two-letter code, '' for a point in no US area, or None if unavailable.

    The three values are DELIBERATELY distinct and callers must keep them
    distinct: '' is an answer ("nowhere in the US"), None is the absence of one
    ("the dataset did not load"). Collapsing them is how a Canadian coordinate
    silently becomes a US state — the defect this module exists to remove.
    """
    try:
        px, py = float(lng), float(lat)
    except (TypeError, ValueError):
        return ""
    if not (-90.0 <= py <= 90.0 and -180.0 <= px <= 180.0):
        return ""
    index = _get_index()
    if not index:
        return None                                # artifact missing; say so
    for code, (min_x, min_y, max_x, max_y), rings in index:
        if not (min_x <= px <= max_x and min_y <= py <= max_y):
            continue
        inside = False
        for (rx0, ry0, rx1, ry1), xs, ys in rings:
            if rx0 <= px <= rx1 and ry0 <= py <= ry1 and _ring_contains(xs, ys, px, py):
                inside = not inside
        if inside:
            return code
    return ""


def boundary_source():
    """Provenance for the geometry, for callers that publish a basis."""
    _get_index()
    return dict(_meta)


def load_error():
    """Why the dataset is unavailable, or None when it loaded."""
    _get_index()
    return _load_error
