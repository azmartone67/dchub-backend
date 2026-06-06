"""Gas pipeline ingest — EIA Natural Gas Pipelines → gas_pipelines table.

The gas_pipelines table was sparse (~918 rows), which thinned the Energy
Discovery "pipelines" layer and the H3 gas sub-score. This ingests the EIA
Natural Gas Pipelines feature service (geo.dot.gov, ~33K polyline features —
the SAME source the map already renders client-side) as representative points.

Safety:
  - Admin-gated (X-Admin-Key / cron secret).
  - Idempotent + NON-DESTRUCTIVE: tags its rows source='eia_geodot_lines' and
    deletes only its own prior rows on each run. The original 918 rows (other
    source) are never touched.
  - Transaction-wrapped (a schema surprise rolls back cleanly, no partial state).
  - Capped + batched so the 1-replica backend isn't overwhelmed.
  - ?dry_run=1 fetches + parses without writing (verify the source safely).
"""
import json
import logging
import os
import urllib.parse
import urllib.request

import psycopg2
from flask import Blueprint, jsonify, request

log = logging.getLogger("gas_pipeline_ingest")
gas_ingest_bp = Blueprint("gas_pipeline_ingest", __name__)

_SRC = "eia_geodot_lines"
_SVC = ("https://geo.dot.gov/server/rest/services/Hosted/"
        "Natural_Gas_Pipelines_US_EIA/FeatureServer/0/query")


def _dsn() -> str:
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


def _admin_ok() -> bool:
    expected = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")
    provided = (
        request.headers.get("X-Admin-Key")
        or request.headers.get("X-Internal-Key")
        or request.args.get("admin_key")
        or ""
    )
    return bool(expected) and provided == expected


def _first_point(geom: dict):
    """Representative point (first vertex) from an ArcGIS polyline geometry."""
    try:
        paths = geom.get("paths") or []
        if paths and paths[0]:
            x, y = paths[0][0][0], paths[0][0][1]
            return float(y), float(x)  # lat, lng
    except Exception:
        pass
    return None, None


def _fetch(cap: int):
    """Paginate the feature service; return [(lat, lng, operator, type), ...]."""
    rows = []
    offset = 0
    page = 1000
    while len(rows) < cap:
        params = urllib.parse.urlencode({
            "where": "1=1",
            "outFields": "operator,typepipe,status",
            "returnGeometry": "true",
            "outSR": "4326",
            "resultOffset": offset,
            "resultRecordCount": page,
            "f": "json",
        })
        with urllib.request.urlopen(_SVC + "?" + params, timeout=30) as r:
            data = json.loads(r.read().decode())
        feats = data.get("features") or []
        if not feats:
            break
        for f in feats:
            lat, lng = _first_point(f.get("geometry") or {})
            if lat is None:
                continue
            a = f.get("attributes") or {}
            rows.append((lat, lng, (a.get("operator") or "")[:200],
                         (a.get("typepipe") or "")[:80]))
        offset += page
        if len(feats) < page:
            break
    return rows[:cap]


@gas_ingest_bp.route("/api/v1/admin/ingest/gas-pipelines", methods=["POST"])
def ingest_gas_pipelines():
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    dsn = _dsn()
    if not dsn:
        return jsonify(ok=False, error="no DATABASE_URL"), 503

    dry = request.args.get("dry_run", "0") == "1"
    try:
        cap = min(int(request.args.get("cap", 12000)), 33000)
    except (TypeError, ValueError):
        cap = 12000

    try:
        rows = _fetch(cap)
    except Exception as e:
        return jsonify(ok=False, error=f"source fetch failed: {str(e)[:160]}"), 502

    if dry:
        return jsonify(ok=True, dry_run=True, fetched=len(rows), sample=rows[:3])

    inserted = 0
    try:
        with psycopg2.connect(dsn, sslmode="require", connect_timeout=8) as c:
            with c.cursor() as cur:
                cur.execute("SELECT column_name FROM information_schema.columns "
                            "WHERE table_name='gas_pipelines'")
                cols = {r[0] for r in cur.fetchall()}
                has_source = "source" in cols
                if not has_source:
                    try:
                        cur.execute("ALTER TABLE gas_pipelines "
                                    "ADD COLUMN IF NOT EXISTS source TEXT")
                        has_source = True
                    except Exception:
                        c.rollback()
                if has_source:
                    cur.execute("DELETE FROM gas_pipelines WHERE source = %s", (_SRC,))

                base = ["lat", "lng", "operator", "pipeline_type"]
                use = [col for col in base if col in cols]
                if has_source:
                    use.append("source")
                collist = ",".join(use)
                ph = "(" + ",".join(["%s"] * len(use)) + ")"

                def _row_vals(t):
                    lat, lng, op, tp = t
                    m = {"lat": lat, "lng": lng, "operator": op,
                         "pipeline_type": tp, "source": _SRC}
                    return tuple(m[col] for col in use)

                batch = []
                for t in rows:
                    batch.append(_row_vals(t))
                    if len(batch) >= 500:
                        args = b",".join(cur.mogrify(ph, b) for b in batch)
                        cur.execute(f"INSERT INTO gas_pipelines ({collist}) VALUES "
                                    + args.decode())
                        inserted += len(batch)
                        batch = []
                if batch:
                    args = b",".join(cur.mogrify(ph, b) for b in batch)
                    cur.execute(f"INSERT INTO gas_pipelines ({collist}) VALUES "
                                + args.decode())
                    inserted += len(batch)
            c.commit()
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200], inserted=inserted), 500

    return jsonify(ok=True, inserted=inserted, source=_SRC)


def register_gas_pipeline_ingest(app):
    """Idempotent registration helper."""
    try:
        app.register_blueprint(gas_ingest_bp)
    except Exception as e:
        log.warning(f"gas_pipeline_ingest registration: {e}")
