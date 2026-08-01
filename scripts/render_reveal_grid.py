#!/usr/bin/env python3
"""render_reveal_grid.py — pre-render the 5 km reVeal grid for a state to R2.

This is the job /api/v1/reveal-grid-export has been waiting for. Until it runs,
that endpoint 404s `not_rendered` for every state — by design (#2093): it HEADs
object storage and refuses to claim readiness it cannot verify. Land an artifact
at the key below and the endpoint starts serving it with no code change.

    reveal-grid-exports/<STATE>/reveal_grid_<STATE>_5km.<format>

WHY THIS IS NOT JUST A LOOP OVER THE LIVE ENDPOINT
--------------------------------------------------
compute_reveal_cell() issues ONE DB round-trip per cell, via
nlr_intelligence._query_substations. At ~0.2 s/cell that is ~24 min for VA
(7,100 cells) and ~3 h for TX (54,300) — and 54,300 bbox queries against the
shared pool is exactly the saturation pattern the pool notes warn about.

Everything else compute_reveal_cell touches is already in memory:
_nearest_geothermal walks the GEOTHERMAL_ZONES constant, and solar/wind/tax are
dict lookups. Substations are the only DB dependency, and a whole state is
small — 8,464 rows for CA, 5,319 for TX, 1,505 for VA out of 126,845 total.

So this loads the substations for the state's bounding box ONCE and swaps
_query_substations for an in-memory equivalent, then calls the REAL
compute_reveal_cell for every cell. All scoring, weighting and provenance stay
in the shipped function — only the data access changes. A second
implementation of the scoring would drift from the live endpoint the first time
either side was edited, and the export would silently stop matching
reveal-cell-bulk.

★ The in-memory lookup must be SEMANTICALLY IDENTICAL to the SQL, including its
quirks, or the export diverges from the live answer:
  · the SQL filters on a lat/lng BOX, then applies a haversine radius after
  · it ORDERs BY manhattan distance (ABS(dlat)+ABS(dlng)) and LIMITs 50
    BEFORE the haversine filter, so a 51st-nearest-by-manhattan row is dropped
    even if it is inside the radius
  · it does NOT filter on the state column at all, despite taking a `state`
    argument — cells near a border legitimately see out-of-state substations
tests/test_render_reveal_grid.py asserts parity against the real query.

Usage
-----
    python3 scripts/render_reveal_grid.py --state VA --formats geojson,csv
    python3 scripts/render_reveal_grid.py --state VA --limit-cells 50 --no-upload
    python3 scripts/render_reveal_grid.py --state VA --formats geojson,csv,parquet

parquet is emitted only when pyarrow is importable; it is NOT a backend
dependency (the API only presigns the object, it never parses it), so install it
in the render job alone.

Env: DATABASE_URL or NEON_DATABASE_URL (read), plus R2_ENDPOINT_URL /
R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_EXPORTS_BUCKET to upload.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

log = logging.getLogger("render_reveal_grid")

CELL_SIZE_KM = 5.0
CAPACITY_MW = 100.0

# Must match reveal_endpoints.GRID_EXPORT_PREFIX / _grid_export_key(). Asserted
# in tests rather than imported, because importing reveal_endpoints drags in
# flask and the whole blueprint for what is a two-token constant.
GRID_EXPORT_PREFIX = "reveal-grid-exports/"


def export_key(state: str, fmt: str) -> str:
    return f"{GRID_EXPORT_PREFIX}{state}/reveal_grid_{state}_5km.{fmt}"


_BOUNDS_SOURCE = "power_plant_intel.py"
_BOUNDS_NAME = "_STATE_BOUNDS"


def _load_state_bounds():
    """Read _STATE_BOUNDS out of the source file WITHOUT importing the module.

    Read, never re-inlined: three copies of this table already exist
    (routes/rankings_routes, ingest_rankings_data, power_plant_intel) and the
    last disagrees with the other two on WI/NH/NJ/ME/MD. All three agree on
    every state this job renders, but a fourth copy would be the next drift.

    ★ It is AST-extracted rather than imported because power_plant_intel does
    `from flask import Blueprint` at module scope, and this job installs only
    psycopg2 / boto3 / pyarrow. `from power_plant_intel import _STATE_BOUNDS`
    worked on a dev box with flask present and died with ModuleNotFoundError
    for all 15 states on the first real workflow run. Pulling a data literal
    should not drag in a web framework.
    """
    import ast
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        _BOUNDS_SOURCE)
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == _BOUNDS_NAME):
            bounds = ast.literal_eval(node.value)
            if not bounds:
                raise SystemExit(f"{_BOUNDS_SOURCE}:{_BOUNDS_NAME} parsed empty")
            return bounds
    raise SystemExit(f"{_BOUNDS_SOURCE} no longer defines {_BOUNDS_NAME}")


def state_bounds(state: str):
    """(min_lat, min_lng, max_lat, max_lng) for a state."""
    bounds = _load_state_bounds()
    if state not in bounds:
        raise SystemExit(f"no bounds for state {state!r}; known: {sorted(bounds)}")
    return bounds[state]


# ---------------------------------------------------------------------------
# In-memory substation lookup — a parity replacement for the per-cell query
# ---------------------------------------------------------------------------

class SubstationIndex:
    """Bucketed in-memory index reproducing _query_substations() exactly."""

    BUCKET_DEG = 1.0

    def __init__(self, rows):
        # rows: (name, voltage_kv, capacity_mva, lat, lng, state)
        self.rows = rows
        self.buckets = {}
        for r in rows:
            key = (math.floor(r[3] / self.BUCKET_DEG), math.floor(r[4] / self.BUCKET_DEG))
            self.buckets.setdefault(key, []).append(r)

    def _candidates(self, lat_min, lat_max, lon_min, lon_max):
        out = []
        for bx in range(math.floor(lat_min / self.BUCKET_DEG),
                        math.floor(lat_max / self.BUCKET_DEG) + 1):
            for by in range(math.floor(lon_min / self.BUCKET_DEG),
                            math.floor(lon_max / self.BUCKET_DEG) + 1):
                out.extend(self.buckets.get((bx, by), ()))
        return out

    def query(self, lat, lon, state=None, radius_km=80):
        """Mirrors nlr_intelligence._query_substations, quirks included.

        `state` is accepted and ignored, exactly as the SQL ignores it.
        """
        from nlr_intelligence import _haversine_km

        lat_range = radius_km / 111.0
        lon_range = radius_km / (111.0 * abs(math.cos(math.radians(lat))) + 0.001)
        lat_min, lat_max = lat - lat_range, lat + lat_range
        lon_min, lon_max = lon - lon_range, lon + lon_range

        box = [r for r in self._candidates(lat_min, lat_max, lon_min, lon_max)
               if lat_min <= r[3] <= lat_max and lon_min <= r[4] <= lon_max]

        # ORDER BY ABS(lat-%s) + ABS(lng-%s) LIMIT 50 — applied BEFORE the
        # haversine filter, so the truncation is part of the contract.
        # Same tie-break as the SQL (see nlr_intelligence._query_substations):
        # manhattan, then lat, lng -- NUMERIC, never `name`, so no text
        # collation sits between this and the SQL. Without a tie-break the
        # two paths order ties differently and subs[:10] sums a different set.
        box.sort(key=lambda r: (abs(r[3] - lat) + abs(r[4] - lon), r[3], r[4]))
        box = box[:50]

        results = []
        for name, voltage_kv, capacity_mva, slat, slon, sstate in box:
            dist = _haversine_km(lat, lon, float(slat or 0), float(slon or 0))
            if dist <= radius_km:
                results.append({
                    "name": name,
                    "voltage_kv": float(voltage_kv or 0),
                    "capacity_mva": float(capacity_mva or 0),
                    "distance_km": round(dist, 1),
                    "state": sstate,
                })
        # Must match _query_substations' final sort exactly.
        results.sort(key=lambda x: (x["distance_km"], x["name"]))
        return results


def load_substations(conn, min_lat, min_lng, max_lat, max_lng, margin_deg=1.5):
    """Every substation in the state box plus a margin.

    The margin matters: a cell on the border legitimately pulls substations
    from the neighbouring state, and the SQL never filters on the state column.
    Loading `WHERE state = %s` would quietly change the answer near borders.
    """
    sql = """
        SELECT name, voltage_kv, capacity_mva,
               -- Must match _query_substations' ::float8 widening exactly; see
               -- the comment there. Reading the bare `real` here would give
               -- Python a shortest-repr value that orders ties differently
               -- from the SQL, changing which 50 rows survive the LIMIT.
               lat::float8, lng::float8, state
        FROM substations
        WHERE lat BETWEEN %(lat_min)s AND %(lat_max)s
          AND lng BETWEEN %(lon_min)s AND %(lon_max)s
    """
    cur = conn.cursor()
    cur.execute(sql, {
        "lat_min": min_lat - margin_deg, "lat_max": max_lat + margin_deg,
        "lon_min": min_lng - margin_deg, "lon_max": max_lng + margin_deg,
    })
    rows = cur.fetchall()
    cur.close()
    return rows


# ---------------------------------------------------------------------------
# Grid
# ---------------------------------------------------------------------------

def tessellate(min_lat, min_lng, max_lat, max_lng, cell_size_km=CELL_SIZE_KM):
    """Cell centroids, matching reveal_cell_bulk's tessellation."""
    cell_deg_lat = cell_size_km / 111.0
    avg_lat = (min_lat + max_lat) / 2.0
    cell_deg_lon = cell_size_km / (111.0 * max(abs(math.cos(math.radians(avg_lat))), 0.1))
    n_lat = int(math.ceil((max_lat - min_lat) / cell_deg_lat))
    n_lon = int(math.ceil((max_lng - min_lng) / cell_deg_lon))
    for i in range(n_lat):
        for j in range(n_lon):
            yield (min_lat + (i + 0.5) * cell_deg_lat,
                   min_lng + (j + 0.5) * cell_deg_lon)


def render_state(state, conn, limit_cells=None, progress_every=500):
    min_lat, min_lng, max_lat, max_lng = state_bounds(state)

    t0 = time.time()
    rows = load_substations(conn, min_lat, min_lng, max_lat, max_lng)
    index = SubstationIndex(rows)
    log.info("loaded %s substations for %s in %.1fs", len(rows), state, time.time() - t0)

    # Swap ONLY the data access. compute_reveal_cell resolves the helper through
    # nlr_intelligence at call time via _safe_import_helpers(), so patching the
    # module attribute is enough and the shipped scoring is untouched.
    import nlr_intelligence
    original = nlr_intelligence._query_substations
    nlr_intelligence._query_substations = index.query
    try:
        from reveal_cell import compute_reveal_cell
        cells, errors = [], 0
        t1 = time.time()
        for n, (lat, lon) in enumerate(tessellate(min_lat, min_lng, max_lat, max_lng), 1):
            if limit_cells and n > limit_cells:
                break
            try:
                body = compute_reveal_cell(lat, lon, CELL_SIZE_KM, state, CAPACITY_MW)
            except Exception as exc:
                log.debug("cell (%s,%s) failed: %s", lat, lon, exc)
                errors += 1
                continue
            if not body.get("success"):
                errors += 1
                continue
            cells.append({
                "cell_id": body.get("cell", {}).get("cell_id"),
                "lat": round(lat, 4),
                "lon": round(lon, 4),
                "state": state,
                "suitability_composite": body.get("suitability_composite"),
                "confidence": body.get("confidence"),
                **{k: v for k, v in (body.get("reveal_features") or {}).items()},
            })
            if progress_every and n % progress_every == 0:
                rate = n / max(time.time() - t1, 1e-9)
                log.info("  %s cells, %.0f cell/s, %s errors", n, rate, errors)
    finally:
        nlr_intelligence._query_substations = original

    log.info("rendered %s cells for %s in %.1fs (%s errors)",
             len(cells), state, time.time() - t1, errors)
    return cells, errors


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

def _field_names(cells):
    seen = []
    for c in cells:
        for k in c:
            if k not in seen:
                seen.append(k)
    return seen


def to_geojson(cells, state):
    return json.dumps({
        "type": "FeatureCollection",
        "properties": {
            "state": state,
            "cell_size_km": CELL_SIZE_KM,
            "cell_count": len(cells),
            "source": "DC Hub reveal-grid-export · pre-rendered via scripts/render_reveal_grid.py",
        },
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [c["lon"], c["lat"]]},
            "properties": {k: v for k, v in c.items() if k not in ("lat", "lon")},
        } for c in cells],
    }).encode()


def to_csv(cells, state):
    if not cells:
        return b""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=_field_names(cells), extrasaction="ignore")
    w.writeheader()
    w.writerows(cells)
    return buf.getvalue().encode()


def to_parquet(cells, state):
    """None when pyarrow is absent — a missing optional format, not a failure."""
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        log.warning("pyarrow not installed — skipping parquet")
        return None
    if not cells:
        return None
    cols = {k: [c.get(k) for c in cells] for k in _field_names(cells)}
    buf = io.BytesIO()
    pq.write_table(pa.table(cols), buf, compression="snappy")
    return buf.getvalue()


SERIALISERS = {"geojson": to_geojson, "csv": to_csv, "parquet": to_parquet}

CONTENT_TYPES = {
    "geojson": "application/geo+json",
    "csv": "text/csv",
    "parquet": "application/vnd.apache.parquet",
}


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

def r2_client():
    if not (os.environ.get("R2_ENDPOINT_URL") and os.environ.get("R2_ACCESS_KEY_ID")):
        return None, None
    import boto3
    client = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT_URL"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ.get("R2_SECRET_ACCESS_KEY", ""),
        region_name="auto",
    )
    return client, (os.environ.get("R2_EXPORTS_BUCKET") or "dchub-daily")


def upload(client, bucket, state, fmt, payload):
    key = export_key(state, fmt)
    client.put_object(Bucket=bucket, Key=key, Body=payload,
                      ContentType=CONTENT_TYPES[fmt])
    # Read back rather than trusting the write — a green put is not proof the
    # endpoint will find it, and the endpoint's whole contract is HEAD-based.
    head = client.head_object(Bucket=bucket, Key=key)
    stored = head.get("ContentLength")
    if stored != len(payload):
        raise SystemExit(f"upload verify FAILED for {key}: put {len(payload)}B, "
                         f"HEAD reports {stored}B")
    log.info("uploaded + verified %s (%s bytes)", key, stored)
    return key, stored


def get_conn():
    import psycopg2
    url = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") \
        or os.environ.get("NEON_REPLICA_URL")
    if not url:
        raise SystemExit("DATABASE_URL / NEON_DATABASE_URL / NEON_REPLICA_URL required")
    return psycopg2.connect(url, connect_timeout=15)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--state", required=True, help="two-letter state code, e.g. VA")
    ap.add_argument("--formats", default="geojson,csv",
                    help="comma-separated: geojson, csv, parquet")
    ap.add_argument("--limit-cells", type=int, default=None,
                    help="stop after N cells (smoke tests)")
    ap.add_argument("--no-upload", action="store_true", help="render only")
    ap.add_argument("--out-dir", default=None, help="also write files here")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s")

    state = args.state.upper()
    formats = [f.strip().lower() for f in args.formats.split(",") if f.strip()]
    bad = [f for f in formats if f not in SERIALISERS]
    if bad:
        raise SystemExit(f"unknown format(s): {bad}; choose from {sorted(SERIALISERS)}")

    conn = get_conn()
    try:
        cells, errors = render_state(state, conn, limit_cells=args.limit_cells)
    finally:
        conn.close()

    if not cells:
        raise SystemExit(f"{state}: rendered 0 cells — refusing to publish an empty grid")

    client, bucket = (None, None) if args.no_upload else r2_client()
    if not args.no_upload and client is None:
        raise SystemExit("R2 not configured (R2_ENDPOINT_URL / R2_ACCESS_KEY_ID); "
                         "pass --no-upload to render without publishing")

    written = []
    for fmt in formats:
        payload = SERIALISERS[fmt](cells, state)
        if payload is None:
            log.warning("%s: %s skipped", state, fmt)
            continue
        if args.out_dir:
            os.makedirs(args.out_dir, exist_ok=True)
            path = os.path.join(args.out_dir, f"reveal_grid_{state}_5km.{fmt}")
            with open(path, "wb") as fh:
                fh.write(payload)
            log.info("wrote %s (%s bytes)", path, len(payload))
        if client:
            key, size = upload(client, bucket, state, fmt, payload)
            written.append({"format": fmt, "key": key, "bytes": size})
        else:
            written.append({"format": fmt, "bytes": len(payload), "uploaded": False})

    print(json.dumps({
        "state": state, "cells": len(cells), "cell_errors": errors,
        "artifacts": written,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
