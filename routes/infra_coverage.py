"""infra_coverage — the dimension the growth board does not have.

★ 2026-09-04. The Land & Power map had NO transmission anywhere outside the US,
for months, and the continuous-infrastructure loop reported healthy throughout.
Not because the loop was broken — because of what it measures.

`routes/infra_growth.py` tracks ROW COUNT PER TABLE OVER TIME. It asks "did
transmission_lines grow?". That table holds ~95,560 rows and grows, so it reads
as one of the healthiest loaders on the board — while being 100% HIFLD, which is
100% United States. Measured the same day against the HIFLD ArcGIS service the
map itself calls: 177 features over N. Virginia, 0 over ALL of Germany, while
OpenStreetMap had 65,460 voltage-tagged power lines for Germany alone.

A US-only table that keeps growing is INDISTINGUISHABLE, on a volume-only board,
from a global one. There is no geography dimension anywhere. Volume monitoring
cannot see a missing continent.

infra_growth.py already documents this exact failure class, twice, without
generalizing it — most sharply on subsea (2026-08-07):

    "the layer was not measured AT ALL, which on the page is indistinguishable
     from a layer measured and found flat."

Geography is that same sentence with a different noun. This module points a
sensor at it.

★ WHAT IT MEASURES, AND WHY THIS SHAPE

No geocoder, no country polygons, no new dependency: bin rows into a coarse
10°x10° grid and report two numbers per layer.

    cells         — how many distinct grid cells hold rows
    concentration — share of rows in the single densest cell

A genuinely global layer spreads over many cells with low concentration. A layer
that is one country wearing a global label collapses into a handful of cells at
high concentration. That distinction is the whole point, and it needs one
GROUP BY rather than a geocoding pipeline.

Each layer also declares the SCOPE THE PRODUCT CLAIMS for it. A finding fires
when declared and measured disagree — a layer served worldwide whose rows are
all in one place. `us`-scoped layers are exempt by declaration: HIFLD being
US-only is correct, and flagging it forever would train everyone to ignore this.

★ NEVER 0

A layer whose geo columns cannot be found is reported `measured: false` with a
reason, and contributes NOTHING to any total. It is never reported as zero
coverage. Collapsing "missing table", "no coordinate columns" and "genuinely
empty" into the same published 0 is the bug infra_growth.py was burned by on
2026-07-29, and repeating it here would make this sensor lie the way the last
one did.

Column names are DISCOVERED against information_schema rather than assumed —
several of these tables could not be resolved from source alone, and a wrong
column name raises UndefinedColumn, which an except-block would turn back into
exactly the silent zero this exists to prevent.
"""
from __future__ import annotations

import logging
import os

import psycopg2
from flask import Blueprint, jsonify, request

log = logging.getLogger("infra_coverage")
infra_coverage_bp = Blueprint("infra_coverage", __name__)

CELL_DEG = 10          # 10°x10° bins — coarse on purpose; this is a shape test
MIN_CELLS = 6          # a global layer should touch more than a country's worth
MAX_CONCENTRATION = 0.90   # >90% of rows in ONE cell is a single-region layer


# label -> candidate (table, lat_col, lng_col) in priority order. The FIRST
# candidate whose columns actually exist is used; if none do, the layer is
# reported unmeasured with the candidates it tried.
_GEO: dict[str, list[tuple[str, str, str]]] = {
    # transmission_lines itself stores NO geometry (live schema verified
    # 2026-07-29: 14 columns, none a coordinate), which is why the map's spatial
    # consumers read the geocoded snapshot instead. Coverage has to be measured
    # where the coordinates are.
    "transmission_lines": [("transmission_lines_eia", "lat", "lng")],
    "substations":        [("substations", "lat", "lng")],
    "gas_pipelines":      [("gas_pipelines", "lat", "lng")],
    "fiber_routes":       [("fiber_routes", "start_lat", "start_lng")],
    "power_plants_eia":   [("power_plants", "lat", "lng"),
                           ("power_plants", "latitude", "longitude")],
    "gas_compressors":    [("gas_compressor_stations", "lat", "lng"),
                           ("gas_compressor_stations", "latitude", "longitude")],
    "gas_processing":     [("gas_processing_plants", "lat", "lng"),
                           ("gas_processing_plants", "latitude", "longitude")],
    "subsea_cables":      [("subsea_landing_points", "lat", "lng"),
                           ("subsea_landing_points", "latitude", "longitude")],
}

# What the PRODUCT claims. `global` layers are asserted; `us` layers are exempt
# by declaration — HIFLD being US-only is correct, and a finding that fires
# forever is one everybody learns to ignore.
_SCOPE: dict[str, str] = {
    "transmission_lines": "global",   # the map serves this worldwide
    "substations":        "global",
    "gas_pipelines":      "us",       # HIFLD/EIA by construction
    "fiber_routes":       "global",
    "power_plants_eia":   "us",       # EIA-860 is a US survey
    "gas_compressors":    "us",
    "gas_processing":     "us",
    "subsea_cables":      "global",
}


def _dsn() -> str:
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


def _admin_ok() -> bool:
    expected = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")
    provided = (request.headers.get("X-Admin-Key")
                or request.headers.get("X-Internal-Key")
                or request.args.get("admin_key") or "")
    return bool(expected) and provided == expected


def _columns(cur, table: str) -> set:
    """Live column set for `table`, or empty if the table does not exist."""
    cur.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
        (table,))
    return {r[0] for r in cur.fetchall()}


def _resolve(cur, label: str):
    """First candidate whose table AND both coordinate columns exist.
    Returns (table, lat, lng) or (None, reason)."""
    tried = []
    for table, lat, lng in _GEO.get(label, []):
        cols = _columns(cur, table)
        tried.append(f"{table}({lat},{lng})")
        if not cols:
            continue
        if lat in cols and lng in cols:
            return (table, lat, lng), None
    if not tried:
        return None, "no geo candidates declared for this layer"
    return None, f"no candidate resolved — tried {', '.join(tried)}"


def _measure(cur, label: str) -> dict:
    """Coverage shape for one layer. Never returns a 0 it cannot justify."""
    resolved, reason = _resolve(cur, label)
    if not resolved:
        return {"label": label, "measured": False, "reason": reason,
                "declared_scope": _SCOPE.get(label)}

    table, lat, lng = resolved
    # Identifiers are from the _GEO literal above, never from user input.
    cur.execute(
        f"""SELECT FLOOR({lat} / {CELL_DEG}) AS clat,
                   FLOOR({lng} / {CELL_DEG}) AS clng,
                   COUNT(*) AS n
              FROM {table}
             WHERE {lat} IS NOT NULL AND {lng} IS NOT NULL
               AND {lat} BETWEEN -90 AND 90
               AND {lng} BETWEEN -180 AND 180
             GROUP BY 1, 2""")
    rows = cur.fetchall()

    total = sum(r[2] for r in rows)
    if total == 0:
        # Table resolved and queried, and it genuinely holds no usable
        # coordinates. That IS a real zero — reported as its own state, not
        # folded in with "we could not measure".
        return {"label": label, "measured": True, "table": table,
                "geocoded_rows": 0, "cells": 0, "concentration": None,
                "declared_scope": _SCOPE.get(label),
                "note": "table resolved but holds no usable coordinates"}

    densest = max(rows, key=lambda r: r[2])
    return {
        "label": label,
        "measured": True,
        "table": table,
        "geocoded_rows": total,
        "cells": len(rows),
        "concentration": round(densest[2] / total, 4),
        "densest_cell": {"lat": int(densest[0]) * CELL_DEG,
                         "lng": int(densest[1]) * CELL_DEG,
                         "rows": densest[2]},
        "declared_scope": _SCOPE.get(label),
    }


def _finding_for(m: dict) -> dict | None:
    """A finding when the measured shape contradicts the declared scope."""
    if not m.get("measured") or m.get("declared_scope") != "global":
        return None
    if m.get("geocoded_rows", 0) == 0:
        return {
            "issue": "layer_scope_contradiction",
            "url": m["label"],
            "count": 1,
            "detail": (f"Layer '{m['label']}' is served as GLOBAL but "
                       f"{m['table']} holds no usable coordinates at all."),
        }
    cells, conc = m["cells"], m["concentration"]
    if cells >= MIN_CELLS and conc <= MAX_CONCENTRATION:
        return None
    d = m["densest_cell"]
    return {
        "issue": "layer_scope_contradiction",
        "url": m["label"],
        "count": 1,
        "detail": (
            f"Layer '{m['label']}' is served as GLOBAL but its rows are "
            f"single-region: {m['geocoded_rows']:,} geocoded rows across only "
            f"{cells} of the {CELL_DEG}°x{CELL_DEG}° cells, {conc:.1%} of them "
            f"in one cell at ({d['lat']},{d['lng']}). A volume-only board reads "
            f"this as healthy because the row count still grows. Either widen "
            f"ingest to the scope claimed, or change the declared scope in "
            f"routes/infra_coverage._SCOPE to what the data actually covers."),
    }


def run_coverage() -> dict:
    dsn = _dsn()
    if not dsn:
        return {"ok": False, "error": "no DATABASE_URL"}
    conn = None
    try:
        conn = psycopg2.connect(dsn)
        cur = conn.cursor()
        layers, findings = [], []
        for label in _GEO:
            try:
                m = _measure(cur, label)
            except Exception as e:
                conn.rollback()
                # Unmeasured, WITH a reason — never a published zero.
                m = {"label": label, "measured": False,
                     "reason": f"{type(e).__name__}: {str(e)[:160]}",
                     "declared_scope": _SCOPE.get(label)}
            layers.append(m)
            f = _finding_for(m)
            if f:
                findings.append(f)
        measured = [l for l in layers if l.get("measured")]
        return {
            "layers": layers,
            "findings": findings,
            "layers_total": len(layers),
            "layers_measured": len(measured),
            "layers_unmeasured": len(layers) - len(measured),
            "cell_deg": CELL_DEG,
            "thresholds": {"min_cells": MIN_CELLS,
                           "max_concentration": MAX_CONCENTRATION},
        }
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _persist(findings: list) -> dict:
    if not findings:
        return {"persisted": 0}
    try:
        from routes.brain_findings_writer import upsert_brain_finding
    except Exception as e:
        return {"persisted": 0, "error": f"import failed: {type(e).__name__}"}
    conn = None
    try:
        conn = psycopg2.connect(_dsn())
        cur = conn.cursor()
        for f in findings:
            upsert_brain_finding(cur, issue=f["issue"], url=f["url"],
                                 count=f.get("count", 1), detail=f.get("detail", ""),
                                 detector="infra_coverage")
        conn.commit()
        return {"persisted": len(findings)}
    except Exception as e:
        return {"persisted": 0, "error": f"{type(e).__name__}: {str(e)[:160]}"}
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@infra_coverage_bp.route("/api/v1/jobs/infra-coverage", methods=["GET", "POST"])
def infra_coverage():
    """Measure per-layer geographic coverage; persist scope contradictions.

    ★ The kill-switch does NOT return ok=True. cron_heartbeat._classify records
    "skipped" only when `ok is not True`, so ok=True would make a DISABLED job
    report success forever — the disarmed-verifier failure this module exists to
    catch a geographic instance of.
    """
    if os.environ.get("INFRA_COVERAGE_DISABLE") == "1":
        return jsonify(skipped="INFRA_COVERAGE_DISABLE=1"), 200

    result = run_coverage()
    if result.get("ok") is False:
        result["error"] = result.get("error") or "coverage run failed"
        return jsonify(result), 200

    result["persist"] = ({"persisted": 0, "note": "dry run"}
                         if request.args.get("dry") == "1"
                         else _persist(result["findings"]))
    n = len(result["findings"])
    if n:
        result["ok"] = False
        result["error"] = ("scope contradiction: "
                           + ", ".join(f["url"] for f in result["findings"]))[:300]
    else:
        result["ok"] = True
    return jsonify(result), 200


@infra_coverage_bp.route("/api/v1/admin/infra-coverage", methods=["GET"])
def infra_coverage_admin():
    """Full per-layer coverage board (admin) — the geography column the growth
    board is missing."""
    if not _admin_ok():
        return jsonify({"error": "admin key required"}), 401
    return jsonify(run_coverage()), 200


def register_infra_coverage(app):
    app.register_blueprint(infra_coverage_bp)
    return True
