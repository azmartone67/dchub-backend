"""Transmission line ingest — EIA Electric Power Transmission Lines → transmission_lines.

The Land & Power "electric transmission" layer FROZE: transmission_lines held a
stale HIFLD snapshot (52,244 rows, source='HIFLD') with no refresh path. Railway's
egress to ArcGIS is unreliable, so — exactly like the proven gas-pipeline ingest —
a GitHub Actions runner (tools/infra_fetch.py) fetches the live EIA service and
POSTs compact attribute rows here, which writes Neon.

Source: EIA US_Electric_Power_Transmission_Lines (FiaPA4ga0iQKduv3 org, ~94,619
features). Fields: ID, TYPE, STATUS, OWNER, VOLTAGE, VOLT_CLASS, SUB_1, SUB_2,
SOURCE, VAL_DATE. The table stores no geometry, but length_miles and state are
DERIVED from it (EPSG:4326, util/polyline_geometry.py) — by the runner, or by
_fetch on the fallback path. Until 2026-09-23 the fetch was attributes-only and
all 94,619 rows landed with state NULL and length_miles 0 (the column default),
so land_power_crawler's per-state transmission_line_count and
total_transmission_miles read 0 everywhere. A row without a measurement is
written as NULL, never 0: 0 miles is a claim, NULL is the absence of one.

Safety:
  - Admin-gated (X-Admin-Key / X-Internal-Key).
  - ★ THE ONLY ROW WRITER of transmission_lines (2026-09-23;
    tests/test_transmission_lines_single_writer.py). land_power_crawler's
    nightly HIFLD upsert was a second one: it re-added 934 superseded 2021
    records after every Monday replace, so the layer flapped 95,569 <-> 94,635
    weekly. That crawl, the news-headline writer in autonomous_brain.py and
    the TRUNCATE loader behind /api/jobs/transmission-refresh are retired.
  - Idempotent FULL-REPLACE inside ONE transaction: deletes the stale HIFLD +
    prior runner rows, then batched INSERT. A schema surprise rolls back cleanly
    so the old rows survive (no partial / empty-layer state). The 94k EIA set
    supersedes the stale 52k HIFLD → clean single-source layer.
  - Dynamic column detection (only insert columns that exist live).
  - Capped + batched (500/exec via cur.mogrify) so the 1-replica backend copes.
  - ?dry_run=1 fetches + parses without writing (verify the source safely).
  - Accepts POST body {"rows":[...]} (preferred, runner-provided) OR a server-side
    _fetch fallback.
"""
import json
import logging
import math
import os
import urllib.parse
import urllib.request

import psycopg2
from flask import Blueprint, jsonify, request

log = logging.getLogger("transmission_ingest")
transmission_ingest_bp = Blueprint("transmission_ingest", __name__)

_SRC = "eia-arcgis-runner"
# Sources superseded by this full-replace (the stale HIFLD snapshot + any prior
# runner-tagged rows). Kept in sync with tools/infra_fetch.py / the workflow.
_DELETE_SOURCES = ("eia-arcgis-runner", "HIFLD", "hifld", "hifld-runner")
# EIA Electric Power Transmission Lines (national, ~94,619 features). Same
# reliable FiaPA4ga0iQKduv3 org the working gas ingest uses.
_SVC = ("https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/"
        "US_Electric_Power_Transmission_Lines/FeatureServer/0/query")

# Row tuple shape (matches tools/infra_fetch.fetch_transmission_lines):
#   (hifld_id, name, operator, voltage_kv, from_sub, to_sub, status, line_type,
#    length_miles, state)
# length_miles + state were appended LAST so an older 8-field row still coerces
# (to NULL, NULL) instead of shifting every column.
_ROW_FIELDS = ("hifld_id", "name", "operator", "voltage_kv",
               "from_sub", "to_sub", "status", "line_type",
               "length_miles", "state")


# Share of rows that must carry BOTH a measured length and a state, or the
# full-replace is refused before any connection opens (the old rows survive).
# Measured 2026-09-23 over the full live layer: 94,619/94,619 lengths,
# 94,617/94,619 states (the 2 misses: a 0.07 mi stub off Harbor Beach, MI, in
# Lake Huron and a tie line in Canada). A geometry request the service ignores,
# or an old 8-field client, measures ~0% and is refused here instead of writing
# another all-NULL layer.
MEASURED_FLOOR = 0.98


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


def _voltage(v):
    """VOLTAGE comes as kV int; -999999 / negatives are 'not available' sentinels."""
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _miles(v):
    """Measured length; None (not 0) for anything that is not a finite, >= 0 number."""
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 3) if math.isfinite(f) and f >= 0 else None


def _state(v):
    """Two-letter code as util/state_polygons emits it; anything else is None."""
    s = str(v or "").strip().upper()
    return s if len(s) == 2 and s.isascii() and s.isalpha() else None


def _clean(s, n):
    if s is None:
        return None
    s = str(s).strip()
    if not s or s.upper() in ("NOT AVAILABLE", "UNKNOWN", "NULL", "NONE"):
        return None
    return s[:n]


def _fetch(cap: int):
    """Paginate the EIA transmission service → list of row tuples (see _ROW_FIELDS).

    Pages of 2000 (service maxRecordCount). Maps OWNER→operator,
    VOLTAGE→voltage_kv, SUB_1/2→from/to_sub, OWNER (or ID)→name, and measures
    the EPSG:4326 geometry into length_miles + state — the same helper the
    runner uses, so the fallback cannot regress to NULL/0 on its own."""
    from util.polyline_geometry import measure_line
    rows = []
    offset = 0
    page = 2000
    while len(rows) < cap:
        params = urllib.parse.urlencode({
            "where": "1=1",
            "outFields": "ID,TYPE,STATUS,OWNER,VOLTAGE,SUB_1,SUB_2",
            "returnGeometry": "true",
            "outSR": "4326",
            "geometryPrecision": "6",
            "resultOffset": offset,
            "resultRecordCount": page,
            "f": "json",
        })
        with urllib.request.urlopen(_SVC + "?" + params, timeout=55) as r:
            data = json.loads(r.read().decode())
        feats = data.get("features") or []
        if not feats:
            break
        for f in feats:
            a = f.get("attributes") or {}
            hid = _clean(a.get("ID"), 64)
            owner = _clean(a.get("OWNER"), 200)
            name = owner or hid
            miles, state = measure_line((f.get("geometry") or {}).get("paths"))
            rows.append((
                hid,
                (name or "")[:200] if name else None,
                owner,
                _voltage(a.get("VOLTAGE")),
                _clean(a.get("SUB_1"), 200),
                _clean(a.get("SUB_2"), 200),
                _clean(a.get("STATUS"), 80),
                _clean(a.get("TYPE"), 80),
                _miles(miles),
                _state(state),
            ))
        offset += page
        if len(feats) < page:
            break
    return rows[:cap]


def _coerce_body_row(r):
    """Normalize a runner-provided row (list/tuple) to the _ROW_FIELDS tuple."""
    if not isinstance(r, (list, tuple)) or len(r) < 1:
        return None
    g = lambda i: r[i] if len(r) > i else None
    return (
        _clean(g(0), 64),
        _clean(g(1), 200),
        _clean(g(2), 200),
        _voltage(g(3)),
        _clean(g(4), 200),
        _clean(g(5), 200),
        _clean(g(6), 80),
        _clean(g(7), 80),
        _miles(g(8)),
        _state(g(9)),
    )


@transmission_ingest_bp.route("/api/v1/admin/ingest/transmission-lines", methods=["POST"])
def ingest_transmission_lines():
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    dsn = _dsn()
    if not dsn:
        return jsonify(ok=False, error="no DATABASE_URL"), 503

    dry = request.args.get("dry_run", "0") == "1"
    try:
        cap = min(int(request.args.get("cap", 100000)), 200000)
    except (TypeError, ValueError):
        cap = 100000

    # Runner-provided rows (preferred): Railway's egress to ArcGIS is unreliable,
    # so the GitHub runner fetches + POSTs. Body: {"rows":[[hifld_id,name,operator,
    # voltage_kv,from_sub,to_sub,status,line_type,length_miles,state],...]},
    # optionally gzipped.
    # Falls back to the server-side _fetch when no body is sent.
    body_rows = None
    raw = request.get_data() or b""
    if raw:
        try:
            enc = (request.headers.get("Content-Encoding") or "").lower()
            if "gzip" in enc or request.headers.get("X-Content-Gzip"):
                import gzip as _gz
                raw = _gz.decompress(raw)
            j = json.loads(raw)
            if isinstance(j, dict) and isinstance(j.get("rows"), list):
                body_rows = []
                for r in j["rows"]:
                    cr = _coerce_body_row(r)
                    if cr is not None:
                        body_rows.append(cr)
        except Exception:
            body_rows = None

    if body_rows is not None:
        rows = body_rows[:cap] if request.args.get("cap") else body_rows
    else:
        try:
            rows = _fetch(cap)
        except Exception as e:
            return jsonify(ok=False, error=f"source fetch failed: {str(e)[:160]}"), 502

    if dry:
        return jsonify(ok=True, dry_run=True, fetched=len(rows), sample=rows[:3])

    if not rows:
        return jsonify(ok=False, error="no rows to ingest (refused empty full-replace)"), 400

    with_len = sum(1 for r in rows if r[8] is not None)
    with_state = sum(1 for r in rows if r[9] is not None)
    if min(with_len, with_state) < MEASURED_FLOOR * len(rows):
        return jsonify(ok=False, error=(
            f"refused full-replace: length_miles on {with_len}/{len(rows)} rows, "
            f"state on {with_state}/{len(rows)} (floor {MEASURED_FLOOR:.0%})")), 400

    inserted = 0
    try:
        with psycopg2.connect(dsn, sslmode="require", connect_timeout=8) as c:
            with c.cursor() as cur:
                cur.execute("SELECT column_name FROM information_schema.columns "
                            "WHERE table_name='transmission_lines'")
                cols = {r[0] for r in cur.fetchall()}

                # Idempotent full-replace: clear stale HIFLD + prior runner rows.
                if "source" in cols:
                    cur.execute(
                        "DELETE FROM transmission_lines WHERE source IN %s",
                        (tuple(_DELETE_SOURCES),))

                use = [col for col in _ROW_FIELDS if col in cols]
                if "source" in cols:
                    use.append("source")
                have_last_updated = "last_updated" in cols
                have_created_at = "created_at" in cols
                tail = []
                if have_last_updated:
                    tail.append("last_updated")
                if have_created_at:
                    tail.append("created_at")
                collist = ",".join(use + tail)
                ph = "(" + ",".join(["%s"] * len(use)) + \
                     "".join([",NOW()"] * len(tail)) + ")"

                def _row_vals(t):
                    m = dict(zip(_ROW_FIELDS, t))
                    m["source"] = _SRC
                    return tuple(m[col] for col in use)

                batch = []
                for t in rows:
                    batch.append(_row_vals(t))
                    if len(batch) >= 500:
                        args = b",".join(cur.mogrify(ph, b) for b in batch)
                        cur.execute(f"INSERT INTO transmission_lines ({collist}) "
                                    f"VALUES " + args.decode())
                        inserted += len(batch)
                        batch = []
                if batch:
                    args = b",".join(cur.mogrify(ph, b) for b in batch)
                    cur.execute(f"INSERT INTO transmission_lines ({collist}) "
                                f"VALUES " + args.decode())
                    inserted += len(batch)
            c.commit()
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200], inserted=inserted), 500

    return jsonify(ok=True, inserted=inserted, source=_SRC)


def register_transmission_ingest(app):
    """Idempotent registration helper."""
    try:
        app.register_blueprint(transmission_ingest_bp)
    except Exception as e:
        log.warning(f"transmission_ingest registration: {e}")
